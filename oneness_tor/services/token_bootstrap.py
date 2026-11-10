"""Bootstrap the top-N market-cap tokens onto the chain — once, idempotently.

The public metadata cache (:mod:`oneness_tor.services.token_media`) already knows
the top coins by market-cap rank, their tickers, names and logos. This module
turns the first ``N`` of them into **real on-chain tokens** with
``meme.create_token`` — the same signed ``token_create`` transaction a visitor
would pay 5 TOR for — so the permanent token table, the meme launcher, the
explorer and the AI all see one set of tickers.

Rules that matter:

* **deterministic** — ranked by market cap (the cache's ``rank``), then symbol,
  so every operator who runs it with the same ``limit``/``supply`` mints the
  same list;
* **idempotent** — a ticker already on the chain is skipped, never re-minted;
* **honest** — every failure is reported per ticker with the chain's message;
  nothing is invented, and ``dry_run`` is the default;
* **replicated** — the tokens are ordinary ledger state, so a replica that
  pulls the origin snapshot shows exactly the same table
  (``POST /api/chain/sync/pull``).
"""
from __future__ import annotations

import threading
import time

from core import pricing
from core.errors import AppError, ChainUnavailable, ValidationError
from oneness_tor.services import meme, token_media

STANDARD = "oneness-tor-token-bootstrap/1"
DEFAULT_LIMIT = 100
MAX_LIMIT = 250
#: One fixed supply for every ticker — the smoke-test default of this endpoint.
#: It is **not** the provider's total supply: a market cap derived from it is
#: wrong. The 120-token fleet (``token_fleet``) mints each asset's own supply.
DEFAULT_SUPPLY = 1_000_000
DEFAULT_POOL_TOR = 10.0
DEFAULT_POOL_SHARE = 10            # pool gets supply / 10 tokens

_LOCK = threading.Lock()
_RUN: dict = {"running": False, "started_at": None, "finished_at": None,
              "total": 0, "done": 0, "created": 0, "failed": 0}


def _clamp(limit: object) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _supply(value: object) -> int:
    try:
        amount = int(float(value))
    except (TypeError, ValueError):
        amount = DEFAULT_SUPPLY
    if amount <= 0 or amount > meme.MAX_SUPPLY:
        raise ValidationError(f"supply must be between 1 and {meme.MAX_SUPPLY}")
    return amount


def source() -> dict:
    """Ranked ``[{symbol, name, rank, image}]`` from the cached public metadata."""
    catalog = token_media.catalog()
    images = catalog.get("images") or {}
    rows: list[dict] = []
    for symbol, entry in images.items():
        if not isinstance(entry, dict):
            continue
        ticker = str(symbol or "").strip().upper()
        if not ticker or not meme.SYMBOL_RE.match(ticker):
            continue
        rank = entry.get("rank")
        rows.append({"symbol": ticker,
                     "name": str(entry.get("name") or ticker)[:80],
                     "rank": int(rank) if isinstance(rank, (int, float)) else None,
                     "coingecko_id": entry.get("coingecko_id"),
                     "image": entry.get("image") or ""})
    rows.sort(key=lambda row: (row["rank"] or 10 ** 6, row["symbol"]))
    return {"rows": rows, "catalog": catalog}


def existing() -> tuple[set[str], str | None]:
    """Tickers already on the ledger (and why we could not ask, if we could not)."""
    try:
        return {str(symbol).upper() for symbol in meme.chain_tokens()}, None
    except (ChainUnavailable, AppError) as problem:
        return set(), getattr(problem, "message", str(problem))


def plan(limit: object = DEFAULT_LIMIT, supply: object = DEFAULT_SUPPLY) -> dict:
    """Exactly what a run would mint — no transaction, no fee, no surprise."""
    wanted = _clamp(limit)
    amount = _supply(supply)
    feed = source()
    if not feed["rows"]:
        raise ValidationError(
            "no public token metadata is cached on this node — refresh it with "
            "POST /api/tokens/images/refresh (admin) or set tokens.images.enabled",
            details={"store": token_media.STORE})
    held, warning = existing()
    rows: list[dict] = []
    for position, row in enumerate(feed["rows"][:wanted], start=1):
        rows.append({**row, "position": position, "supply": amount,
                     "exists": row["symbol"] in held})
    pending = [row for row in rows if not row["exists"]]
    create_fee = float(pricing.price("meme_token_create") or 0)
    catalog = feed["catalog"]
    return {
        "standard": STANDARD, "limit": wanted, "supply": amount,
        "source": {"count": len(feed["rows"]),
                   "standard": catalog.get("standard"),
                   "fetched_at": catalog.get("fetched_at"),
                   "stale": catalog.get("stale"),
                   "store": catalog.get("store")},
        "tokens": rows,
        "to_create": len(pending),
        "existing": len(rows) - len(pending),
        "fee": {"code": "meme_token_create", "tor_each": create_fee,
                "tor_total": round(create_fee * len(pending), 8),
                "charged_by": "the Rust chain (token_create)"},
        "warning": warning,
        "progress": dict(_RUN),
    }


def status() -> dict:
    """The last/current run, plus the live plan for the default limit."""
    report: dict = {"standard": STANDARD, "run": dict(_RUN)}
    try:
        report["plan"] = {key: value for key, value in plan().items()
                          if key in ("limit", "supply", "to_create", "existing",
                                     "fee", "source", "warning")}
    except AppError as problem:
        report["plan"] = {"error": problem.message}
    return report


def run(*, limit: object = DEFAULT_LIMIT, supply: object = DEFAULT_SUPPLY,
        wallet: object = "founder", dry_run: bool = True, pools: bool = False,
        pool_tor: object = DEFAULT_POOL_TOR,
        pool_share: object = DEFAULT_POOL_SHARE) -> dict:
    """Mint the planned tokens (and optionally seed pools), resuming safely."""
    document = plan(limit, supply)
    owner = str(wallet or "founder").strip() or "founder"
    if dry_run:
        return {**document, "dry_run": True, "wallet": owner,
                "created": [], "failed": [],
                "skipped": [row["symbol"] for row in document["tokens"]
                            if row["exists"]],
                "next": ("POST /api/tokens/bootstrap {dry_run:false} to mint, "
                         "then POST /api/chain/sync/pull on every replica")}
    from oneness_tor.services import node_sync

    node_sync.require_writable()
    if not _LOCK.acquire(blocking=False):
        raise ValidationError("a token bootstrap run is already in progress")
    started = time.time()
    pool_amount = max(1, document["supply"] // max(1, int(pool_share or 1)))
    created: list[dict] = []
    failed: list[dict] = []
    skipped: list[str] = []
    _RUN.update(running=True, started_at=started, finished_at=None,
                total=document["to_create"], done=0, created=0, failed=0)
    try:
        for row in document["tokens"]:
            if row["exists"]:
                skipped.append(row["symbol"])
                continue
            try:
                result = meme.create_token(symbol=row["symbol"], name=row["name"],
                                           supply=document["supply"], wallet=owner)
            except AppError as problem:
                failed.append({"symbol": row["symbol"], "stage": "token",
                               "code": problem.code, "message": problem.message})
                _RUN.update(done=_RUN["done"] + 1, failed=len(failed))
                continue
            entry = {"symbol": row["symbol"], "name": row["name"],
                     "rank": row["rank"], "txid": result.get("txid"),
                     "height": result.get("height")}
            if pools:
                try:
                    seeded = meme.create_pool(symbol=row["symbol"],
                                              token_amount=pool_amount,
                                              tor_amount=pool_tor, wallet=owner)
                    entry["pool"] = seeded.get("pool")
                except AppError as problem:
                    failed.append({"symbol": row["symbol"], "stage": "pool",
                                   "code": problem.code,
                                   "message": problem.message})
            created.append(entry)
            _RUN.update(done=_RUN["done"] + 1, created=len(created))
        return {
            **{key: document[key] for key in
               ("standard", "limit", "supply", "source", "fee", "warning")},
            "dry_run": False, "wallet": owner, "pools": bool(pools),
            "created": created, "created_count": len(created),
            "skipped": skipped, "failed": failed,
            "elapsed_seconds": round(time.time() - started, 1),
            "replicate": ("POST /api/chain/sync/pull on every replica (origin "
                          "snapshot) to show the same tokens there"),
            "next": "GET /api/tokens/public",
        }
    finally:
        _RUN.update(running=False, finished_at=time.time())
        _LOCK.release()

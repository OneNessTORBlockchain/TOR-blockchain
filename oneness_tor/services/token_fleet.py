"""120 wallets, one token each — the market universe minted as real ledger state.

The chain keys a token by its ticker (``rust/torchain``), so a ticker can only
ever be created once. This module therefore mints the universe onto a ledger
where those tickers do not exist yet, and it says so before spending anything:

* **one wallet per token** — :func:`ledger.new_wallet` writes a genuine Ed25519
  keystore (``data/wallets/<prefix>NNN.json``, gitignored) and the founder funds
  it, so it can pay the chain's own 5 TOR ``token_create`` fee itself;
* **the feed's own numbers** — the ticker, the name and the *total supply*
  published by the winning provider are what the chain stores (see
  :mod:`oneness_tor.services.market_feed`);
* **one picture** — the feed's ``image`` goes to :func:`meme.create_token`
  (launcher metadata) *and* into :func:`token_media.remember` (the cache the token
  table, the launcher, the wallet page and the explorer all read), so the same
  logo is drawn everywhere and never expires;
* **honest reporting** — every failure is recorded per asset with the chain's own
  message, a dry run is the default, and a run is resumable (an existing wallet
  or ticker is reused, never duplicated).

Two records come out of a run: ``data/metadata/token-fleet.json`` (machine) and
``docs/TOKEN_FLEET.md`` (human). Both list what is public — wallet name, address,
public key, ticker, name, supply, price, market cap, logo — and neither ever
contains a secret key: the key material stays in ``data/wallets/``, which git
ignores.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core import pricing
from core.config import settings
from core.errors import AppError, ChainUnavailable, ValidationError
from core.validator import format_tor, to_micro
from oneness_tor.services import ledger, market_feed, meme, token_media
from oneness_tor.services.store import data_dir, read_json, safe_name, write_json

STANDARD = "oneness-tor-token-fleet/1"
DEFAULT_MAJORS = market_feed.DEFAULT_MAJORS
DEFAULT_MEMES = market_feed.DEFAULT_MEMES
#: Enough for the chain's 5 TOR create fee plus head-room for the transfer fee.
DEFAULT_FUND_TOR = 6.0
DEFAULT_FOUNDER = "founder"
DEFAULT_PREFIX = "fleet"
#: Wallet numbering of the first generation (``fleet001``); a re-mint passes the
#: next free number so every token lands in a brand-new key pair.
DEFAULT_START = 1
MAX_ASSETS = 250
MANIFEST = "data/metadata/token-fleet.json"
DOCUMENT = "docs/TOKEN_FLEET.md"

_LOCK = threading.Lock()
_RUN: dict = {"running": False, "started_at": None, "finished_at": None,
              "total": 0, "done": 0, "created": 0, "failed": 0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _document_path() -> Path:
    """``docs/TOKEN_FLEET.md`` at the repository root."""
    return Path(__file__).resolve().parents[2] / DOCUMENT


def _manifest_path() -> Path:
    return data_dir("metadata") / "token-fleet.json"


def wallet_name(prefix: object, position: int) -> str:
    """``("fleet", 7)`` → ``fleet007`` (a legal keystore name)."""
    root = str(prefix or DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
    return safe_name(f"{root}{int(position):03d}", "wallet prefix")


def _start(value: object) -> int:
    """The first wallet number of this run (``1`` when unset)."""
    try:
        first = int(value)
    except (TypeError, ValueError):
        first = DEFAULT_START
    if first < 0:
        raise ValidationError("start must be 0 or a positive wallet number",
                              details={"received": value})
    return first


def _archive_path(number: int) -> Path:
    """``data/metadata/token-fleet/gen-01.json`` — a superseded generation."""
    return data_dir("metadata") / "token-fleet" / f"gen-{int(number):02d}.json"


def _label(path: Path) -> str:
    """A stable label for a record path: repo-relative when it lives in the repo."""
    try:
        return path.resolve().relative_to(Path(__file__).resolve().parents[2]).as_posix()
    except ValueError:
        return str(path)


def _generation_start(stored: dict) -> int | None:
    """The wallet number the stored record was minted from (``None`` unknown).

    A generation is identified by where its wallet numbering starts: the record
    says ``start: 121`` for the re-mint that produced ``fleet121…fleet240``. An
    older record without that field still answers, because every entry names its
    wallet (``wallet_index``, or the digits in the keystore name).
    """
    try:
        if stored.get("start") is not None:
            return int(stored["start"])
    except (TypeError, ValueError):
        pass
    numbers: list[int] = []
    for entry in stored.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("wallet_index")
        if raw is None:
            digits = "".join(ch for ch in str((entry.get("wallet") or {})
                                             .get("name") or "") if ch.isdigit())
            raw = digits or None
        try:
            numbers.append(int(raw))
        except (TypeError, ValueError):
            continue
    return min(numbers) if numbers else None


def _next_start() -> int:
    """The first free wallet number — where a *new* generation would begin."""
    numbers: list[int] = []
    for entry in manifest().get("entries") or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("wallet_index")
        if raw is None:
            digits = "".join(ch for ch in str((entry.get("wallet") or {})
                                             .get("name") or "") if ch.isdigit())
            raw = digits or None
        try:
            numbers.append(int(raw))
        except (TypeError, ValueError):
            continue
    return (max(numbers) + 1) if numbers else DEFAULT_START


def _generation() -> int:
    """The generation the stored manifest records (``0`` when there is none)."""
    try:
        return int(manifest().get("generation") or 0)
    except (TypeError, ValueError):
        return 0


def _fund_micro(value: object) -> int:
    try:
        tor = float(value)
    except (TypeError, ValueError):
        tor = DEFAULT_FUND_TOR
    fee = float(pricing.price("meme_token_create") or 0)
    floor = fee + 1.0 if fee else 1.0
    return to_micro(max(floor, tor))


def _chain_tickers() -> tuple[set[str], str | None]:
    """Tickers already on the ledger (and why we could not ask, if we could not)."""
    try:
        return {str(symbol).upper() for symbol in meme.chain_tokens()}, None
    except (ChainUnavailable, AppError) as problem:
        return set(), getattr(problem, "message", str(problem))


def _founder_view(founder: str) -> dict:
    try:
        account = ledger.balance_of(founder)
    except (ChainUnavailable, AppError, OSError) as problem:
        return {"wallet": founder, "balance_micro": None, "balance_tor": None,
                "warning": getattr(problem, "message", str(problem))}
    micro = int(account.get("balance_micro") or 0)
    return {"wallet": founder, "address": account.get("address"),
            "balance_micro": micro, "balance_tor": format_tor(micro)}


# ------------------------------------------------------------------------ plan
def plan(*, majors: object = DEFAULT_MAJORS, memes: object = DEFAULT_MEMES,
         fund_tor: object = DEFAULT_FUND_TOR, founder: object = DEFAULT_FOUNDER,
         prefix: object = DEFAULT_PREFIX, start: object = DEFAULT_START) -> dict:
    """Exactly what a run would do — no wallet, no transaction, no fee.

    ``start`` numbers the wallets: ``start=121`` mints the same 120 assets into
    ``fleet121…fleet240``, i.e. 120 **new** key pairs and addresses. A generation
    is what a re-mint is: the old keystores stay on disk (and in the archived
    manifest) untouched.
    """
    owner = str(founder or DEFAULT_FOUNDER).strip() or DEFAULT_FOUNDER
    first = _start(start)
    feed = market_feed.universe(majors=majors, memes=memes)
    rows = market_feed.rows_for(feed, majors, memes)
    if not rows:
        raise ValidationError(
            "the market feed is empty on this node — refresh it with "
            "POST /api/tokens/universe/refresh (admin) or enable "
            "tokens.universe.enabled",
            details={"store": market_feed.STORE,
                     "errors": feed.get("errors") or []})
    held, warning = _chain_tickers()
    fund = _fund_micro(fund_tor)
    mint_fee = float(pricing.price("meme_token_create") or 0)
    transfer_fee = float(pricing.price("transfer") or 0)
    tokens: list[dict] = []
    for position, row in enumerate(rows, start=1):
        number = first + position - 1
        name = wallet_name(prefix, number)
        exists = row["symbol"] in held
        supply = row.get("supply")
        reason = ""
        if exists:
            reason = "this ticker already exists on the chain — a ticker is minted once"
        elif not supply:
            reason = "the provider published no supply for this asset"
        tokens.append({
            "position": position, "wallet_index": number, "list": row.get("list"),
            "symbol": row["symbol"], "name": row["name"],
            "supply": supply, "supply_basis": row.get("supply_basis"),
            "price_usd": row.get("price_usd"),
            "market_cap_usd": row.get("market_cap_usd"),
            "image": row.get("image"), "image_source": row.get("image_source"),
            "source": row.get("source"), "source_id": row.get("source_id"),
            "token_address": row.get("token_address"),
            "wallet": name,
            "wallet_exists": (ledger.wallet_dir() / f"{name}.json").exists(),
            "exists": exists, "ready": not reason, "blocked": reason,
        })
    pending = [row for row in tokens if row["ready"]]
    fresh = [row for row in pending if not row["wallet_exists"]]
    mint_total = len(pending) * to_micro(mint_fee)
    fund_each = fund + to_micro(transfer_fee)
    # worst case: every pending wallet has to be funded (a reused keystore may
    # already hold TOR on this ledger, but only the node knows — so say the max)
    estimated = mint_total + len(pending) * fund_each
    founder_view = _founder_view(owner)
    affordable = (founder_view.get("balance_micro") is None
                  or int(founder_view["balance_micro"]) >= estimated)
    return {
        "standard": STANDARD, "majors_requested": feed.get("majors_requested"),
        "memes_requested": feed.get("memes_requested"),
        "generation": max(1, _generation()), "start": first,
        "count": len(tokens), "tokens": tokens,
        "to_mint": len(pending), "existing": len(tokens) - len(pending),
        "blocked": [{"symbol": row["symbol"], "reason": row["blocked"]}
                    for row in tokens if not row["ready"]],
        "fee": {"code": "meme_token_create", "tor_each": mint_fee,
                "charged_by": "the Rust chain (token_create)",
                "transfer_tor_each": transfer_fee},
        "funding": {"tor_each": round(fund / 1e8, 8),
                    "wallets_to_create": len(fresh),
                    "wallets_reused": len(pending) - len(fresh),
                    "from": owner},
        "estimated_tor": round(estimated / 1e8, 8),
        "estimated_tor_min": round((mint_total + len(fresh) * fund_each) / 1e8, 8),
        "estimate_basis": ("estimated_tor assumes every pending wallet needs its "
                           "funding transfer; estimated_tor_min counts only the "
                           "wallets that do not exist yet"),
        "founder": founder_view, "affordable": affordable,
        "source": {"store": market_feed.STORE,
                   "count": feed.get("count"),
                   "fetched_at": feed.get("fetched_at"),
                   "stale": feed.get("stale"),
                   "majors": (feed.get("majors") or {}).get("source"),
                   "memes": (feed.get("memes") or {}).get("source"),
                   "errors": feed.get("errors") or []},
        "images": {"with_image": sum(1 for row in tokens if row["image"]),
                   "without_image": sum(1 for row in tokens if not row["image"])},
        "warning": warning, "progress": dict(_RUN),
        "next": ("dry run — POST /api/tokens/fleet {dry_run:false} "
                 "(or tools/mint_fleet.py --run) to create the wallets and mint"),
    }


# ------------------------------------------------------------------- one wallet
def _ensure_wallet(name: str) -> dict:
    """The keystore for ``name``, created through the Rust binary when missing."""
    if not (ledger.wallet_dir() / f"{name}.json").exists():
        ledger.new_wallet(name, words=24)
    return ledger.load_wallet(name)


def _fund(address: str, name: str, founder: str, fund_micro: int) -> dict:
    """Top the wallet up from the founder so it can pay the chain's own fee."""
    account = ledger.node_account(address)
    balance = int(account.get("balance_micro") or 0)
    if balance >= fund_micro:
        return {"funded": False, "balance_micro": balance,
                "reason": "already funded"}
    missing = fund_micro - balance
    result = ledger.send_micro(founder, address, missing,
                               note=f"fleet funding {name}")
    return {"funded": True, "amount_micro": missing,
            "amount_tor": format_tor(missing), "txid": result.get("txid"),
            "height": result.get("height")}


def _description(row: dict) -> str:
    supply = f"{int(row.get('supply') or 0):,}"
    text = (f"{row['name']} ({row['symbol']}) — minted on the OneNess TOR chain "
            f"with the public total supply of {supply} reported by "
            f"{row.get('source') or 'public market data'}. Price and market cap are "
            f"public metadata, not chain state; this node quotes every price in TOR.")
    return text[:600]


def _mint_one(row: dict, *, founder: str, fund_micro: int,
              pin_metadata: bool) -> dict:
    """Create the wallet, fund it, mint its one token, and return the record."""
    name = row["wallet"]
    wallet = _ensure_wallet(name)
    address = str(wallet.get("address") or "")
    funding = _fund(address, name, founder, fund_micro)
    result = meme.create_token(symbol=row["symbol"], name=row["name"],
                               supply=row["supply"],
                               description=_description(row),
                               image=row.get("image") or "", wallet=name,
                               pin_metadata=bool(pin_metadata))
    launcher = result.get("launcher") if isinstance(result.get("launcher"), dict) else {}
    return {
        "position": row["position"], "wallet_index": row.get("wallet_index"),
        "list": row["list"],
        "symbol": row["symbol"], "name": row["name"],
        "supply": row["supply"], "supply_basis": row.get("supply_basis"),
        "price_usd": row.get("price_usd"),
        "market_cap_usd": row.get("market_cap_usd"),
        "image": row.get("image"), "image_source": row.get("image_source"),
        "logo": launcher.get("logo") or "",
        "source": row.get("source"), "source_id": row.get("source_id"),
        "token_address": row.get("token_address"),
        "wallet": {"name": name, "address": address,
                   "pubkey_base58": wallet.get("pubkey_base58"),
                   "key_file": f"data/wallets/{name}.json",
                   "funding": funding},
        "txid": result.get("txid"), "height": result.get("height"),
        "fee": result.get("fee"), "metadata": result.get("metadata"),
        "minted_at": _now(),
    }


# ------------------------------------------------------------------------- run
def run(*, majors: object = DEFAULT_MAJORS, memes: object = DEFAULT_MEMES,
        fund_tor: object = DEFAULT_FUND_TOR, founder: object = DEFAULT_FOUNDER,
        prefix: object = DEFAULT_PREFIX, start: object = DEFAULT_START,
        dry_run: bool = True,
        pin_metadata: bool = False, document: bool = True) -> dict:
    """Create one wallet per asset and mint one token in each (resumable)."""
    document_plan = plan(majors=majors, memes=memes, fund_tor=fund_tor,
                         founder=founder, prefix=prefix, start=start)
    owner = str(founder or DEFAULT_FOUNDER).strip() or DEFAULT_FOUNDER
    if dry_run:
        return {**document_plan, "dry_run": True, "created": [], "created_count": 0,
                "failed": [], "skipped": [], "manifest": None, "document": None,
                "images_remembered": None}
    from oneness_tor.services import node_sync

    node_sync.require_writable()
    if not _LOCK.acquire(blocking=False):
        raise ValidationError("a token fleet run is already in progress")
    started = time.time()
    fund_micro = _fund_micro(fund_tor)
    created: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    _RUN.update(running=True, started_at=started, finished_at=None,
                total=document_plan["to_mint"], done=0, created=0, failed=0)
    try:
        for row in document_plan["tokens"]:
            if not row["ready"]:
                skipped.append({"symbol": row["symbol"], "reason": row["blocked"]})
                continue
            try:
                entry = _mint_one(row, founder=owner, fund_micro=fund_micro,
                                  pin_metadata=pin_metadata)
            except AppError as problem:
                failed.append({"symbol": row["symbol"], "stage": "token",
                               "code": problem.code, "message": problem.message})
                _RUN.update(done=_RUN["done"] + 1, failed=len(failed))
                continue
            created.append(entry)
            _RUN.update(done=_RUN["done"] + 1, created=len(created))
        manifest = _record(created, start=document_plan["start"])
        remembered = token_media.remember({
            entry["symbol"]: {"image": entry["image"],
                              "source": f"market feed ({entry.get('source')})",
                              "rank": entry.get("position"),
                              "name": entry["name"]}
            for entry in created if entry.get("image")})
        written = str(write_document()) if document else ""
        return {
            **{key: document_plan[key] for key in
               ("standard", "generation", "start", "count", "to_mint", "existing",
                "fee", "funding", "estimated_tor", "source", "warning")},
            "dry_run": False, "founder": owner,
            "created": created, "created_count": len(created),
            "failed": failed, "skipped": skipped,
            "manifest": {"store": MANIFEST, "count": manifest["count"],
                         "wallets": manifest["wallets"],
                         "generation": manifest.get("generation"),
                         "superseded": manifest.get("superseded") or [],
                         "totals": manifest["totals"]},
            "document": written,
            "images_remembered": remembered,
            "elapsed_seconds": round(time.time() - started, 1),
            "next": ("GET /api/tokens/fleet for the record, /api/tokens/public for "
                     "the logos; POST /api/chain/sync/pull on every replica so the "
                     "same 120 tokens show there"),
        }
    finally:
        _RUN.update(running=False, finished_at=time.time())
        _LOCK.release()


# --------------------------------------------------------------------- records
def _record(created: list[dict], *, start: int = DEFAULT_START) -> dict:
    """Merge this run into the manifest — the same ticker is never listed twice.

    A run that starts its wallet numbering somewhere else is a **new generation**
    (a re-mint): the previous record is frozen in
    ``data/metadata/token-fleet/gen-<n>.json`` and the entry set starts fresh, so
    the document always describes the tokens that are actually on the chain. A
    run with the same numbering is a resume and merges into that generation.
    """
    path = _manifest_path()
    stored = read_json(path, {}) or {}
    previous = {str(entry.get("symbol")): entry
                for entry in (stored.get("entries") or [])
                if isinstance(entry, dict)}
    previous_start = _generation_start(stored)
    continuing = bool(previous) and previous_start is not None \
        and int(previous_start) == int(start)
    replaced = [entry for entry in created
                if str(entry["symbol"]) in previous]
    superseded = list(stored.get("superseded") or [])
    generation = max(1, _generation())
    if previous and not continuing:
        previous_generation = max(1, int(stored.get("generation") or 1))
        archive = _archive_path(previous_generation)
        write_json(archive, stored)
        superseded.append({
            "generation": previous_generation,
            "at": stored.get("updated_at"),
            "store": _label(archive),
            "count": int(stored.get("count") or len(previous)),
            "wallets": int(stored.get("wallets") or 0),
            "replaced": len(replaced),
            "reason": ("superseded by a re-mint: the same tickers now live in new "
                       "wallets with the provider's total supply"),
        })
        generation = previous_generation + 1
        entries: dict[str, dict] = {}
    else:
        entries = dict(previous)
    for entry in created:
        entries[str(entry["symbol"])] = entry
    rows = sorted(entries.values(),
                  key=lambda entry: (str(entry.get("list") or ""),
                                     int(entry.get("position") or 0)))
    wallets = {str((entry.get("wallet") or {}).get("name"))
               for entry in rows if isinstance(entry.get("wallet"), dict)}
    fee_each = float(pricing.price("meme_token_create") or 0)
    document = {
        "standard": STANDARD, "updated_at": _now(), "store": MANIFEST,
        "generation": generation, "start": int(start),
        "superseded": superseded,
        "chain": {"chain_id": str(settings.get("chain.chain_id")),
                  "node": str(settings.get("node.api_url"))},
        "count": len(rows), "wallets": len(wallets), "entries": rows,
        "totals": {
            "majors": sum(1 for entry in rows
                          if entry.get("list") == market_feed.MAJORS_LIST),
            "memes": sum(1 for entry in rows
                         if entry.get("list") == market_feed.MEMES_LIST),
            "with_image": sum(1 for entry in rows if entry.get("image")),
            "supply": sum(int(entry.get("supply") or 0) for entry in rows),
            "market_cap_usd": round(sum(float(entry.get("market_cap_usd") or 0)
                                        for entry in rows), 2),
            "fee_tor": round(fee_each * len(rows), 8),
            "funded_tor": round(sum(
                int(((entry.get("wallet") or {}).get("funding") or {})
                    .get("amount_micro") or 0) for entry in rows) / 1e8, 8),
        },
        "documents": {"markdown": DOCUMENT, "logos": token_media.STORE,
                      "market_feed": market_feed.STORE},
        "keys": ("secret keys live in data/wallets/<wallet>.json (gitignored); "
                 "this manifest and the markdown record never contain them"),
    }
    write_json(path, document)
    return document


def manifest() -> dict:
    """The stored manifest (``data/metadata/token-fleet.json``), or ``{}``."""
    stored = read_json(_manifest_path(), {}) or {}
    return stored if isinstance(stored, dict) else {}


def _clean(value: object) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def write_document() -> Path:
    """Write ``docs/TOKEN_FLEET.md`` — the human record of all 120 wallets."""
    from oneness_tor.services import token_registry

    stored = manifest()
    rows = [entry for entry in (stored.get("entries") or []) if isinstance(entry, dict)]
    totals = stored.get("totals") or {}
    chain = stored.get("chain") or {}
    generation = int(stored.get("generation") or 1)
    superseded = [row for row in (stored.get("superseded") or [])
                  if isinstance(row, dict)]
    lines = [
        "# The 120-token fleet — one wallet, one token",
        "",
        f"Written {stored.get('updated_at') or _now()} by",
        "`oneness_tor/services/token_fleet.py` on chain",
        f"`{chain.get('chain_id') or settings.get('chain.chain_id')}`",
        f"(node `{chain.get('node') or settings.get('node.api_url')}`) —",
        f"**generation {generation}**.",
        "",
        "The 120 assets are the **top 100 by market cap** plus the **top 20 Solana",
        "memecoins**, fetched from key-less public providers with two fallbacks each",
        "(`oneness_tor/services/market_feed.py`: CoinGecko → CoinPaprika → CoinLore",
        "for the majors; CoinGecko's `solana-meme-coins` category → GeckoTerminal →",
        "DexScreener for the memecoins).",
        "",
        "Every token is a real `token_create` on the ledger: a freshly generated",
        "Ed25519 wallet (funded from the founder) signs it and pays the chain's own",
        "5 TOR fee, the on-chain supply is the provider's **total supply**, and the",
        "logo is the provider's image — remembered in this node's logo cache at mint",
        "and mirrored into `data/metadata/token-logos/`, so the token document, the",
        "token table, the launcher, the wallet page and the explorer draw the same",
        "local file at `/assets/tokens/<SYMBOL>.<ext>` (no third-party image host).",
        "",
        "| generation | tokens | wallets | logos | supply minted | fee paid (TOR) |",
        "| --- | --- | --- | --- | --- | --- |",
        (f"| {generation} | {stored.get('count') or len(rows)} | "
         f"{stored.get('wallets') or 0} | "
         f"{totals.get('with_image') or 0} | {totals.get('supply') or 0:,} | "
         f"{totals.get('fee_tor') or 0:g} |"),
        "",
        "Machine-readable twin: `data/metadata/token-fleet.json` — logo cache:",
        "`data/metadata/token-images.json` — logo mirror:",
        "`data/metadata/token-logos/` — market data cache:",
        "`data/metadata/market-universe.json`. A superseded generation is frozen in",
        "`data/metadata/token-fleet/gen-<n>.json` (never deleted, never merged).",
        "",
        "## The 120 wallets, keys and tokens",
        "",
        "`address`, `pubkey` and `mint address` are the public halves of each token:",
        "the wallet's Ed25519 public key, the address derived from it, and the ledger",
        "token id (`tor:<TICKER>`). The secret half stays in",
        "`data/wallets/<wallet>.json`, which git ignores. Never copy that file into a",
        "document, an issue or a chat.",
        "",
        "| # | ticker | name | supply | price (USD) | market cap (USD) | wallet | address | pubkey | mint address | logo |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in rows:
        wallet = entry.get("wallet") if isinstance(entry.get("wallet"), dict) else {}
        price = entry.get("price_usd")
        market_cap = entry.get("market_cap_usd")
        image = _clean(entry.get("image"))
        mint, _kind = token_registry.mint_address(entry.get("symbol"))
        price_text = ("—" if price is None
                      else f"{float(price):,.8f}".rstrip("0").rstrip("."))
        cap_text = "—" if market_cap is None else f"{float(market_cap):,.0f}"
        lines.append(
            f"| {entry.get('position') or ''} | {_clean(entry.get('symbol'))} "
            f"| {_clean(entry.get('name'))} | {int(entry.get('supply') or 0):,} "
            f"| {price_text} | {cap_text} "
            f"| {_clean(wallet.get('name'))} | {_clean(wallet.get('address'))} "
            f"| {_clean(wallet.get('pubkey_base58'))} "
            f"| `{_clean(mint)}` "
            f"| {'—' if not image else f'[image]({image})'} |")
    if superseded:
        lines += [
            "",
            "## Superseded generations",
            "",
            "A generation is superseded when the same tickers are minted again into",
            "new wallets (a re-mint), because a ticker exists once per chain and the",
            "on-chain supply cannot be edited. Each frozen record keeps the old",
            "wallets, addresses, public keys and mint transactions for audit.",
            "",
            "| generation | written | tokens | wallets | replaced here | frozen record |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for old in superseded:
            lines.append(
                f"| {old.get('generation') or ''} | {_clean(old.get('at'))} "
                f"| {int(old.get('count') or 0):,} | {int(old.get('wallets') or 0)} "
                f"| {int(old.get('replaced') or 0)} "
                f"| `{_clean(old.get('store'))}` |")
    lines += [
        "",
        "## How to verify",
        "",
        "```bash",
        "GET /api/tokens/fleet        # this record, straight from the node",
        "GET /api/public/tokens       # supply, owner, mint address and logo per token",
        "GET /api/tokens/images       # the exact image URL each token is drawn with",
        "POST /api/tokens/images/mirror  # copy every logo onto this node (admin)",
        "GET /assets/tokens/BTC.png   # what the pages actually draw",
        "GET /api/tokens/universe     # the market data and which provider answered",
        "```",
        "",
        "A replica shows the same 120 tokens after `POST /api/chain/sync/pull` — the",
        "tokens are ordinary ledger state.",
        "",
        "Prices and market caps describe the real-world asset a ticker refers to: they",
        "are public metadata, **not** chain state, and every price this node quotes",
        "on-chain is denominated in TOR.",
        "",
    ]
    path = _document_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def status() -> dict:
    """The last/current run, the stored manifest and the live plan."""
    report: dict = {"standard": STANDARD, "store": MANIFEST, "run": dict(_RUN)}
    try:
        live = plan(start=_next_start())
        report["plan"] = {key: live[key] for key in
                          ("generation", "start", "count", "to_mint", "existing",
                           "estimated_tor", "affordable", "funding", "fee",
                           "source", "warning", "images")}
    except AppError as problem:
        report["plan"] = {"error": problem.message}
    stored = manifest()
    report["manifest"] = {"count": stored.get("count"),
                          "wallets": stored.get("wallets"),
                          "generation": stored.get("generation"),
                          "superseded": stored.get("superseded") or [],
                          "updated_at": stored.get("updated_at"),
                          "totals": stored.get("totals"),
                          "document": stored.get("documents") or {}}
    return report


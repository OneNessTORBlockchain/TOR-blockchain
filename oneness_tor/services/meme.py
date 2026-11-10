"""Meme coin launcher — real on-chain tokens, pools and swaps on TOR.

Everything that changes state is a genuine signed transaction on the Rust
ledger (``token_create`` 5 TOR, ``pool_create`` 10 TOR charged by the chain,
``swap`` with the pool's own 0.30% fee). This module adds what the chain does
not model: the launch metadata, paid promotions, volume sessions, AI landing
pages and the DEX seams.

Honest split:

* ``create_token`` / ``create_pool`` / ``swap`` / ``quote`` — **live**, executed
  by ``torchain`` and verifiable in ``/api/chain/events``.
* ``volume session`` — the 10 TOR is charged and the session is stored as
  ``queued``; the trading engine is **not deployed**, so ``run`` answers
  ``ENGINE_NOT_DEPLOYED`` and never reports fake trades.
* external DEX routes (``meme.dex_quote`` / ``meme.dex_swap``) — **placeholders**
  answering ``DEX_NOT_DEPLOYED`` until those pools exist; the link parser is
  real and pure.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from core import ipfs, pricing
from core.config import settings
from core.errors import (AppError, ChainUnavailable, DexNotDeployed,
                         EngineNotDeployed, FeeError, NotFound, ValidationError)
from core.node_client import node
from core.validator import format_tor, to_micro
from oneness_tor.services import builder, ledger, token_media, unwrap
from oneness_tor.services.store import Collection, data_dir

TOKENS = Collection(lambda: data_dir("meme") / "tokens.json", "meme")
PROMOTIONS = Collection(lambda: data_dir("meme") / "promotions.json", "promo")
SESSIONS = Collection(lambda: data_dir("meme") / "sessions.json", "vol")

SWAP_FEE_BPS = 30
MAX_SUPPLY = 2 ** 64 - 1
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")
IMAGE_PREFIXES = ("http://", "https://", "ipfs://", "ar://", "data:image/")
MAX_DAY_PROMOTION = 30
#: How every launcher listing is ordered (the table, the board, the dashboard).
RANKED_BY = ("active promotion, then newest mint, then pool liquidity, then supply")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp(value: object) -> int:
    """A unix mint time (0 when the ledger record carries none)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------- registry
def _symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not SYMBOL_RE.match(text):
        raise ValidationError(
            "symbol must be 2-10 ASCII letters/digits (it is the on-chain ticker)",
            details={"received": text[:24]})
    return text


def chain_tokens() -> dict:
    """``{SYMBOL: token}`` straight from the ledger."""
    return unwrap(node.tokens(), default="the node did not return its tokens")


def _drop_symbol_records(ticker: str) -> int:
    """Delete launcher rows for one ticker — the chain keys a token by its symbol.

    A re-mint (a fresh chain, or the next generation of the fleet) writes a new
    ``token_create`` for the same ticker; the old launcher row then describes a
    token that no longer exists on chain, and keeping it would double every
    listing. Returns how many rows were removed.
    """
    rows = TOKENS.all()
    kept = [row for row in rows
            if str(row.get("symbol") or "").strip().upper() != ticker]
    removed = len(rows) - len(kept)
    if removed:
        TOKENS.save(kept)
    return removed


def chain_pools() -> dict:
    return unwrap(node.pools(), default="the node did not return its pools")


def _token_or_404(symbol: str) -> dict:
    tokens = chain_tokens()
    token = tokens.get(symbol)
    if not token:
        raise NotFound(f"token {symbol} does not exist on this chain",
                       details={"available": sorted(tokens)[:40],
                                "fix": "create it with POST /api/meme/tokens"})
    return token


def _record(symbol: str) -> dict | None:
    return next((row for row in TOKENS.all()
                 if str(row.get("symbol")) == symbol), None)


def metadata_public(symbol: str) -> dict:
    """Launcher metadata (description, image, socials) as the launcher published it.

    Nothing on this node expires on a timer: the launcher row stays readable for
    as long as the token trades. The only thing this node ever hides is the
    sending address of a transaction whose wallet chose *anonymous*.

    The **pinned token document** (``metadata_uri`` → IPFS) is separate: it is
    public like the token table itself, so a listing that only knows the ticker
    can still resolve name/supply/owner/image.
    """
    record = _record(symbol)
    if not record:
        return {"public": False, "expires_at": None,
                "reason": "no launcher metadata"}
    pinned = {"metadata_uri": record.get("metadata_uri") or None,
              "metadata_cid": record.get("metadata_cid") or None,
              "metadata_url": record.get("metadata_url") or None,
              "metadata_pinned": bool(record.get("metadata_pinned")),
              "metadata_standard": (record.get("metadata")
                                    if isinstance(record.get("metadata"), dict)
                                    else {}).get("standard")}
    return {"public": True, "expires_at": None,
            "reason": "launcher metadata is kept — nothing expires",
            **pinned}


def public_images() -> dict[str, str]:
    """Launcher images: ``SYMBOL → image``.

    The image belongs to the launcher row, which no longer expires; a token
    without a launcher image still falls back to its public metadata logo (see
    :mod:`oneness_tor.services.token_media`).
    """
    images: dict[str, str] = {}
    for record in TOKENS.all():
        symbol = str(record.get("symbol") or "").strip().upper()
        image = str(record.get("image") or "").strip()
        if not symbol or not image:
            continue
        images[symbol] = image
    return images


def _pool_view(symbol: str) -> tuple[dict | None, dict | None]:

    pool = chain_pools().get(symbol)
    if not pool:
        return None, None
    token_reserve = int(pool.get("token_reserve") or 0)
    tor_reserve = int(pool.get("tor_reserve") or 0)
    view = {**pool,
            "tor_reserve_tor": format_tor(tor_reserve),
            "price_tor": (round((tor_reserve / 1e8) / token_reserve, 12)
                          if token_reserve else None),
            "liquidity_tor": round(tor_reserve / 1e8, 8)}
    return pool, view


def active_promotions(symbol: str | None = None) -> list[dict]:
    now = int(time.time())
    rows = []
    for row in PROMOTIONS.all():
        if row.get("status") != "active":
            continue
        if int(row.get("ends_unix") or 0) <= now:
            continue
        if symbol and row.get("symbol") != symbol:
            continue
        rows.append(row)
    rows.sort(key=lambda row: int(row.get("ends_unix") or 0), reverse=True)
    return rows


# --------------------------------------------------------------------- tools
def tools() -> dict:
    """Every launcher tool with its real state (never a fake 'ready')."""
    rows = [
        {"id": "create_token", "label": "Create a meme coin",
         "endpoint": "meme.token_create", "method": "POST",
         "cost_tor": pricing.price("meme_token_create"), "ready": True,
         "engine": "torchain tx kind=token_create (fee charged by the chain)"},
        {"id": "token_manager", "label": "Token manager / status",
         "endpoint": "meme.token", "method": "GET", "cost_tor": 0.0,
         "ready": True, "engine": "live ledger state"},
        {"id": "pool_create", "label": "Launch a liquidity pool",
         "endpoint": "meme.pool_create", "method": "POST",
         "cost_tor": pricing.price("meme_pool_create"), "ready": True,
         "engine": "torchain tx kind=pool_create (fee charged by the chain)"},
        {"id": "swap", "label": "Buy / sell on the pool",
         "endpoint": "meme.swap", "method": "POST", "cost_tor": 0.0,
         "ready": True, "engine": "torchain tx kind=swap (0.30% pool fee)"},
        {"id": "quote", "label": "Constant-product quote",
         "endpoint": "meme.quote", "method": "GET", "cost_tor": 0.0,
         "ready": True, "engine": "same integer math as the chain"},
        {"id": "market", "label": "Market stats / chart data",
         "endpoint": "meme.market", "method": "GET", "cost_tor": 0.0,
         "ready": True, "engine": "live pool reserves + public swaps"},
        {"id": "volume_bot", "label": "Volume & market bot — 15-minute session",
         "endpoint": "meme.volume_session_create", "method": "POST",
         "cost_tor": pricing.price("meme_volume_session"), "ready": False,
         "engine": "session record only — trading engine not deployed",
         "placeholder": "ENGINE_NOT_DEPLOYED"},
        {"id": "landing_page", "label": "AI token landing page (built + hosted)",
         "endpoint": "meme.landing", "method": "POST",
         "cost_tor": pricing.price("meme_landing_page"), "ready": True,
         "engine": "Ollama builder + /sites/<id>/ preview"},
        {"id": "trending", "label": "Trending promotion — per day",
         "endpoint": "meme.promote", "method": "POST",
         "cost_tor": pricing.price("meme_trending_day"), "ready": True,
         "engine": "paid promotion board"},
        {"id": "banner", "label": "Banner advertisement — 7 days",
         "endpoint": "meme.promote", "method": "POST",
         "cost_tor": pricing.price("meme_banner_7d"), "ready": True,
         "engine": "paid promotion board"},
        {"id": "dex_venues", "label": "External DEX venues",
         "endpoint": "meme.dex_venues", "method": "GET", "cost_tor": 0.0,
         "ready": False, "engine": "not deployed",
         "placeholder": "DEX_NOT_DEPLOYED"},
        {"id": "dex_quote", "label": "External DEX quote",
         "endpoint": "meme.dex_quote", "method": "GET", "cost_tor": 0.0,
         "ready": False, "engine": "not deployed",
         "placeholder": "DEX_NOT_DEPLOYED"},
        {"id": "dex_swap", "label": "External DEX swap",
         "endpoint": "meme.dex_swap", "method": "POST", "cost_tor": 0.0,
         "ready": False, "engine": "not deployed",
         "placeholder": "DEX_NOT_DEPLOYED"},
        {"id": "dex_link", "label": "DEX link parser",
         "endpoint": "meme.dex_link", "method": "GET", "cost_tor": 0.0,
         "ready": True, "engine": "pure URL parsing (no network)"},
    ]
    return {"tools": rows, "ready": sum(1 for row in rows if row["ready"]),
            "placeholders": sum(1 for row in rows if not row["ready"]),
            "note": "Placeholders answer 501 with their real code; nothing is "
                    "simulated and no price is invented."}


# --------------------------------------------------------------- promo codes
def promo_catalog() -> dict:
    raw = settings.get("meme.promo_codes") or ""
    pairs: list[tuple] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        pairs = [(str(item), 100) for item in raw]
    else:
        for part in str(raw).split(","):
            if not part.strip():
                continue
            code, _, percent = part.partition(":")
            pairs.append((code, percent or 100))
    codes = []
    for code, value in pairs:
        try:
            percent = int(float(str(value).strip().rstrip("%")))
        except (TypeError, ValueError):
            percent = 100
        codes.append({"code": str(code).strip().upper(),
                      "discount_percent": max(0, min(100, percent))})
    return {"codes": codes, "count": len(codes),
            "note": "promo codes discount API-layer fees only — the Rust chain "
                    "charges token_create/pool_create fees itself"}


def find_promo(code: object) -> dict | None:
    wanted = str(code or "").strip().upper()
    if not wanted:
        return None
    return next((row for row in promo_catalog()["codes"]
                 if row["code"] == wanted), None)


def _charge(wallet: str, code: str, *, quantity: float = 1, note: str = "",
            promo: dict | None = None) -> dict:
    """Charge a price code on-chain (promo discount applied when valid)."""
    spec = pricing.spec(code)
    gross = pricing.micro(code, quantity)
    discount = int((promo or {}).get("discount_percent") or 0)
    discount = max(0, min(100, discount))
    amount = gross - gross * discount // 100
    if amount <= 0:
        return {"code": code, "label": spec["label"], "tor": 0.0,
                "amount_micro": 0, "amount_tor": "0", "fee_receiver": None,
                "txid": None, "height": None, "waived": True,
                "promo_code": (promo or {}).get("code")}
    account = ledger.balance_of(wallet)
    balance = int(account.get("balance_micro") or 0)
    if balance < amount:
        raise FeeError(
            f"{wallet} holds {format_tor(balance)} TOR but {code} costs "
            f"{format_tor(amount)} TOR",
            details={"wallet": wallet, "balance_micro": balance,
                     "required_micro": amount, "code": code})
    receiver = node.fee_receiver()
    result = ledger.send_micro(wallet, receiver, amount,
                               note=f"{code}: {note}".strip().rstrip(":"))
    return {"code": code, "label": spec["label"], "tor": amount / 10 ** 8,
            "amount_micro": amount, "amount_tor": format_tor(amount),
            "fee_receiver": receiver, "waived": False,
            "promo_code": (promo or {}).get("code"), **result}


# --------------------------------------------------------------------- quote
def quote(symbol: object, side: object = "buy", amount: object = None) -> dict:
    """Constant-product quote — byte-for-byte the chain's integer math."""
    ticker = _symbol(symbol)
    direction = str(side or "buy").strip().lower()
    if direction not in ("buy", "sell"):
        raise ValidationError("side must be 'buy' or 'sell'")
    pool, view = _pool_view(ticker)
    if not pool:
        raise NotFound(
            f"{ticker} has no liquidity pool yet — launch one with "
            f"POST /api/meme/pools",
            details={"symbol": ticker})
    if amount in (None, ""):
        raise ValidationError("amount is required (TOR for buy, tokens for sell)")
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValidationError("amount must be a number")
    if value <= 0:
        raise ValidationError("amount must be greater than zero")
    tor_reserve = int(pool.get("tor_reserve") or 0)
    token_reserve = int(pool.get("token_reserve") or 0)
    fee_bps = int(pool.get("fee_bps") or SWAP_FEE_BPS)
    if direction == "buy":
        micro = to_micro(value)
        fee = micro * fee_bps // 10_000
        net = micro - fee
        out = token_reserve * net // (tor_reserve + net) if net else 0
        if out <= 0:
            raise ValidationError("that buy is too small to fill",
                                 details={"amount_tor": value})
        return {"symbol": ticker, "side": direction, "amount_in": value,
                "unit_in": "TOR", "amount_micro_in": micro,
                "amount_out": out, "unit_out": ticker,
                "fee_micro": fee, "fee_tor": format_tor(fee),
                "fee_bps": fee_bps, "price_tor": view["price_tor"],
                "pool": view, "executable": True, "endpoint": "meme.swap"}
    tokens_in = int(value)
    if tokens_in <= 0 or tokens_in != value:
        raise ValidationError("sell amount must be a whole number of tokens")
    gross = tor_reserve * tokens_in // (token_reserve + tokens_in)
    fee = gross * fee_bps // 10_000
    out = gross - fee
    if out <= 0:
        raise ValidationError("that sell is too small to fill",
                              details={"amount_tokens": tokens_in})
    return {"symbol": ticker, "side": direction, "amount_in": tokens_in,
            "unit_in": ticker, "amount_out": out, "unit_out": "microTOR",
            "amount_out_tor": format_tor(out), "amount_micro_out": out,
            "fee_micro": fee, "fee_tor": format_tor(fee), "fee_bps": fee_bps,
            "price_tor": view["price_tor"], "pool": view,
            "executable": True, "endpoint": "meme.swap"}


# --------------------------------------------------------------- create token
def create_token(*, symbol: object, name: object = "", supply: object = None,
                 description: object = "", image: object = "",
                 wallet: str = "founder", socials: object = None,
                 promo_code: object = "", pin_metadata: bool = True) -> dict:
    """Create a real on-chain token (the chain charges 5 TOR itself).

    The launch metadata is also pinned to IPFS (Pinata) as a permanent public
    document, so a wallet, DEX listing or marketplace that only knows the
    ticker can still read name/supply/owner/image. A pin failure never fails
    the token: the row keeps an honest ``metadata_pinned: false`` + error.
    """
    ticker = _symbol(symbol)
    title = str(name or "").strip() or ticker
    if len(title) > 80:
        raise ValidationError("name must be at most 80 characters")
    try:
        amount = int(float(supply))
    except (TypeError, ValueError):
        raise ValidationError("supply must be a whole number of tokens")
    if amount <= 0 or amount > MAX_SUPPLY:
        raise ValidationError(f"supply must be between 1 and {MAX_SUPPLY}")
    text = str(description or "").strip()[:600]
    picture = str(image or "").strip()
    if picture and not picture.startswith(IMAGE_PREFIXES):
        raise ValidationError("image must start with http://, https://, ipfs://, "
                              "ar:// or data:image/")
    links: list[str] = []
    if isinstance(socials, list):
        links = [str(item).strip()[:200] for item in socials
                 if str(item).strip()][:8]
    tokens = chain_tokens()
    if ticker in tokens:
        raise ValidationError(f"token {ticker} already exists on chain",
                              details={"owner": tokens[ticker].get("owner")})
    owner = ledger.load_wallet(str(wallet or "founder"))
    address = str(owner.get("address") or "")
    fee = pricing.micro("meme_token_create")
    account = ledger.balance_of(str(wallet or "founder"))
    balance = int(account.get("balance_micro") or 0)
    if balance < fee:
        raise FeeError(
            f"{wallet} holds {format_tor(balance)} TOR but creating a token costs "
            f"{pricing.price('meme_token_create'):g} TOR (charged by the chain)",
            details={"wallet": wallet, "balance_micro": balance,
                     "required_micro": fee, "code": "meme_token_create"})
    promo = find_promo(promo_code)
    result = ledger.send_micro(str(wallet or "founder"), address, amount,
                               token=ticker, kind="token_create", note=title)
    token = chain_tokens().get(ticker, {})
    image_cid = ""
    if picture.startswith("data:image/"):
        uploaded = ipfs.pin_data_url(picture, filename=f"{ticker}-image")
        if uploaded.get("pinned"):
            picture = uploaded["url"]
            image_cid = uploaded.get("cid") or ""
    # the picture this mint names is copied onto this node right away, so the
    # token document below, the token table, the wallet page and the explorer all
    # resolve one logo at /assets/tokens/ — never a third-party image host
    logo = ""
    if picture:
        try:
            token_media.remember({ticker: {"image": picture,
                                           "source": "launcher image",
                                           "name": title}})
            logo = token_media.served_logo(ticker)
        except OSError:      # a logo can never fail a mint that is already on chain
            logo = ""
    _drop_symbol_records(ticker)      # one launcher row per ticker (re-mint safe)
    row = TOKENS.add({
        "symbol": ticker, "name": title, "supply": amount,
        "description": text, "image": picture, "logo": logo, "socials": links,
        "wallet": str(wallet or "founder"), "owner": address,
        "txid": result.get("txid"), "height": result.get("height"),
        "status": "created", "pool": None, "landing_site": None,
        "promo_code": (promo or {}).get("code"),
        "created_at": _now(), "created_unix": int(time.time()),
        "fee": {"code": "meme_token_create",
                "tor": pricing.price("meme_token_create"),
                "charged_by": "the Rust chain (token_create)"},
    })
    metadata = {
        "standard": "oneness-token-metadata/1",
        "name": title, "symbol": ticker, "description": text,
        "image": picture, "external_url": (links[0] if links else ""),
        "properties": {
            "chain": "oneness-tor",
            "chain_id": str(settings.get("chain.chain_id")),
            "supply": amount,
            "decimals": 0,
            "owner": address,
            "image_cid": image_cid,
            "image_mirror": logo or None,
            "mint_address": token.get("mint_address") or ticker,
            "mint_address_kind": token.get("mint_address_kind") or "symbol",
            "created_txid": result.get("txid"),
            "created_height": result.get("height"),
            "created_at": row["created_at"],
            "socials": links,
        },
    }
    pinned = (ipfs.pin_json(metadata, name=f"{ticker}-token-metadata.json")
              if pin_metadata else {"pinned": False, "provider": "pinata",
                                    "reason": "pin_metadata=false"})
    row = TOKENS.update(str(row["id"]), metadata=metadata,
                        metadata_uri=pinned.get("uri") or "",
                        metadata_cid=pinned.get("cid") or "",
                        metadata_url=pinned.get("url") or "",
                        metadata_pinned=bool(pinned.get("pinned")),
                        metadata_error=pinned.get("error"))
    return {"token": token, "launcher": row, "txid": result.get("txid"),
            "height": result.get("height"), "promo": promo,
            "fee": row["fee"],
            "metadata": {"uri": row.get("metadata_uri"),
                         "cid": row.get("metadata_cid"),
                         "url": row.get("metadata_url"),
                         "pinned": row.get("metadata_pinned"),
                         "provider": "pinata", "error": row.get("metadata_error"),
                         "standard": metadata["standard"]},
            "next": "POST /api/meme/pools {symbol, token_amount, tor_amount} "
                    "to launch liquidity"}


# ---------------------------------------------------------------- create pool
def create_pool(*, symbol: object, token_amount: object = None,
                tor_amount: object = None, wallet: str = "founder") -> dict:
    """Seed a real AMM pool (the chain charges 10 TOR and moves the reserves)."""
    ticker = _symbol(symbol)
    _token_or_404(ticker)
    try:
        tokens_in = int(float(token_amount))
    except (TypeError, ValueError):
        raise ValidationError("token_amount must be a whole number of tokens")
    if tokens_in <= 0:
        raise ValidationError("token_amount must be greater than zero")
    try:
        tor_value = float(tor_amount)
    except (TypeError, ValueError):
        raise ValidationError("tor_amount must be a number of TOR")
    if tor_value <= 0:
        raise ValidationError("tor_amount must be greater than zero")
    tor_micro = to_micro(tor_value)
    pools = chain_pools()
    if ticker in pools:
        raise ValidationError(f"a {ticker} pool already exists",
                              details={"pool": pools[ticker]})
    owner = ledger.load_wallet(str(wallet or "founder"))
    address = str(owner.get("address") or "")
    account = ledger.node_account(address)
    held = int((account.get("tokens") or {}).get(ticker) or 0)
    if held < tokens_in:
        raise FeeError(
            f"{wallet} holds {held} {ticker} but the pool needs {tokens_in}",
            details={"wallet": wallet, "held": held, "needed": tokens_in})
    fee = pricing.micro("meme_pool_create")
    balance = int(account.get("balance_micro") or 0)
    if balance < tor_micro + fee:
        raise FeeError(
            f"{wallet} holds {format_tor(balance)} TOR but the pool needs "
            f"{format_tor(tor_micro)} TOR of liquidity + "
            f"{format_tor(fee)} TOR fee",
            details={"wallet": wallet, "balance_micro": balance,
                     "liquidity_micro": tor_micro, "fee_micro": fee,
                     "code": "meme_pool_create"})
    result = ledger.send_micro(str(wallet or "founder"), address, tokens_in,
                               token=ticker, kind="pool_create",
                               note=format_tor(tor_micro))
    _pool, view = _pool_view(ticker)
    record = _record(ticker)
    if record:
        TOKENS.update(str(record.get("id")), pool=view,
                      pool_txid=result.get("txid"), status="pool_live")
    return {"pool": view, "txid": result.get("txid"),
            "height": result.get("height"), "wallet": wallet,
            "fee": {"code": "meme_pool_create",
                    "tor": pricing.price("meme_pool_create"),
                    "charged_by": "the Rust chain (pool_create)"}}


# ---------------------------------------------------------------------- swap
def swap(*, symbol: object, side: object = "buy", amount: object = None,
         wallet: str = "founder") -> dict:
    """Broadcast a real swap; ``quote`` is the same math the chain will apply."""
    ticker = _symbol(symbol)
    direction = str(side or "buy").strip().lower()
    preview = quote(ticker, direction, amount)
    owner = ledger.load_wallet(str(wallet or "founder"))
    address = str(owner.get("address") or "")
    amount_value = (int(preview["amount_micro_in"]) if direction == "buy"
                    else int(preview["amount_in"]))
    result = ledger.send_micro(str(wallet or "founder"), address, amount_value,
                               token=ticker, kind="swap", note=direction)
    _pool, view = _pool_view(ticker)
    record = _record(ticker)
    if record:
        TOKENS.update(str(record.get("id")),
                      last_swap={"side": direction, "at": _now(),
                                 "txid": result.get("txid")})
    return {"swap": {"symbol": ticker, "side": direction,
                     "amount_in": preview["amount_in"],
                     "unit_in": preview["unit_in"],
                     "expected_out": preview["amount_out"],
                     "unit_out": preview["unit_out"]},
            "quote": preview, "pool": view, "wallet": wallet,
            "address": address, "txid": result.get("txid"),
            "height": result.get("height"), "records": result.get("records")}


# ------------------------------------------------------------ status / market
def market(symbol: object) -> dict:
    """Live price, liquidity, market cap, 24h volume and recent swaps."""
    from oneness_tor.services import token_registry

    ticker = _symbol(symbol)
    token = _token_or_404(ticker)
    pool_row, view = _pool_view(ticker)
    reply = node.public_records(500)
    records = (reply.get("data") or {}).get("records") or []
    swaps = [row for row in records
             if row.get("kind") == "swap" and row.get("token") == ticker]
    buy_micro = sum(int(row.get("amount") or 0) for row in swaps
                    if str(row.get("note") or "").startswith("buy"))
    sell_tokens = sum(int(row.get("amount") or 0) for row in swaps
                      if str(row.get("note") or "").startswith("sell"))
    day = token_registry.volume_24h(records, {ticker: pool_row or {}}).get(ticker, {})
    supply = int(token.get("supply") or 0)
    price = view["price_tor"] if view else None
    mint, mint_kind = token_registry.mint_address(ticker)
    return {
        "symbol": ticker, "indexed": bool(view), "token": token, "pool": view,
        "mint_address": mint, "mint_address_kind": mint_kind,
        "price_tor": price,
        "liquidity_tor": view["liquidity_tor"] if view else 0.0,
        "market_cap_tor": (round(price * supply, 8) if price and supply else None),
        "volume_24h_tor": round(float(day.get("tor") or 0.0), 8),
        "volume_24h_swaps": int(day.get("swaps") or 0),
        "volume": {"swaps_in_window": len(swaps),
                   "buy_micro": buy_micro, "buy_tor": format_tor(buy_micro),
                   "sell_tokens": sell_tokens,
                   "window": "every record the chain holds (nothing expires)"},
        "chart": {"kind": "pool-reserves", "embed": None,
                  "note": "price comes from live reserves; a candlestick indexer "
                          "is not deployed, so no fake candles are drawn"},
        "promotions": active_promotions(ticker),
        "updated_at": int(time.time()),
    }


def token_status(symbol: object) -> dict:
    """One token: ledger record, pool, market, supply totals and public holders.

    Balances are public ledger data: the holder list (and every balance) is
    served by :mod:`oneness_tor.services.public_ledger` at
    ``/api/public/tokens/<SYMBOL>/holders``.
    """
    from oneness_tor.services import public_ledger

    ticker = _symbol(symbol)
    token = _token_or_404(ticker)
    _pool, view = _pool_view(ticker)
    meta = metadata_public(ticker)
    record = _record(ticker)
    public_meta = record if (record and meta["public"]) else None
    try:
        ledger_row = public_ledger.token(ticker)
    except (ChainUnavailable, AppError, OSError) as problem:
        ledger_row = {"warning": str(getattr(problem, "message", problem))}
    try:
        holder_report = public_ledger.holders(ticker, 100)
    except (ChainUnavailable, AppError, OSError) as problem:
        holder_report = {"holders_total": None, "holders": [],
                         "warning": str(getattr(problem, "message", problem))}
    return {"symbol": ticker, "token": token, "pool": view,
            "market": market(ticker), "launcher": public_meta,
            "metadata": meta,
            "supply": ledger_row.get("supply", token.get("supply")),
            "circulating": ledger_row.get("circulating"),
            "in_pools": ledger_row.get("in_pools"),
            "held": ledger_row.get("held"),
            "holders_total": holder_report.get("holders_total"),
            "holders": holder_report.get("holders") or [],
            "owner": {"address": str(token.get("owner") or "")},
            "promotions": active_promotions(ticker),
            "landing_site": (public_meta or {}).get("landing_site"),
            "holders_endpoint": f"/api/public/tokens/{ticker}/holders",
            "balances_endpoint": "/api/public/balances",
            "warning": ledger_row.get("warning")}


def listing(limit: int = 50) -> dict:
    """Every on-chain token: price, pools, market cap, 24h volume, owner.

    Launcher metadata (description, image, socials, landing page) is attached as
    the launcher published it: nothing expires, and the token table stays public
    forever.
    """
    from oneness_tor.services import public_ledger, token_registry

    tokens = chain_tokens()
    pools = chain_pools()
    # one public-ledger read answers every row's totals and holder count
    ledger_rows = (public_ledger.snapshot(public_ledger.MAX_LIMIT, 0).get("tokens")
                   or {})
    store = {str(row.get("symbol")): row for row in TOKENS.all()}
    try:
        reply = node.public_records(500)
        recent = ((reply or {}).get("data") or {}).get("records") or []
    except Exception:  # noqa: BLE001 - a listing must not fail on a node hiccup
        recent = []
    volumes = token_registry.volume_24h(recent, pools)
    rows = []
    for ticker, token in tokens.items():
        _pool, view = _pool_view(ticker)
        meta = metadata_public(ticker)
        record = store.get(ticker)
        public_meta = record if (record and meta["public"]) else None
        price = view["price_tor"] if view else None
        supply = int(token.get("supply") or 0)
        volume = volumes.get(ticker, {})
        ledger_row = ledger_rows.get(ticker) or {}
        mint, mint_kind = token_registry.mint_address(ticker)
        rows.append({
            "symbol": ticker, "name": token.get("name"),
            "supply": supply, "owner": token.get("owner"),
            "circulating": ledger_row.get("circulating"),
            "in_pools": ledger_row.get("in_pools"),
            "holders": ledger_row.get("holders"),
            "created_at": token.get("created_at"),
            "mint_address": mint, "mint_address_kind": mint_kind,
            "description": (public_meta or {}).get("description") or "",
            "image": ipfs.resolve((public_meta or {}).get("image") or ""),
            "image_uri": (public_meta or {}).get("image") or "",
            "metadata_uri": meta.get("metadata_uri"),
            "metadata_cid": meta.get("metadata_cid"),
            "metadata_url": meta.get("metadata_url"),
            "pool": view, "pools": [view] if view else [],
            "price_tor": price,
            "liquidity_tor": view["liquidity_tor"] if view else 0.0,
            "market_cap_tor": round(price * supply, 8) if price and supply else None,
            "volume_24h_tor": round(float(volume.get("tor") or 0.0), 8),
            "volume_24h_swaps": int(volume.get("swaps") or 0),
            "promoted": bool(active_promotions(ticker)),
            "landing_site": (public_meta or {}).get("landing_site"),
            "launcher": public_meta,
            "metadata": {**meta, "public": bool(public_meta)},
        })
    rows.sort(key=lambda row: (row["promoted"], _stamp(row["created_at"]),
                               row["liquidity_tor"], int(row["supply"] or 0)),
              reverse=True)
    from oneness_tor.services import token_media
    token_media.decorate(rows, launcher_images=public_images())
    return {"tokens": rows[:max(1, int(limit))], "count": len(rows),
            "ranked_by": RANKED_BY,

            "prices": {"token_create_tor": pricing.price("meme_token_create"),
                       "pool_create_tor": pricing.price("meme_pool_create"),
                       "swap_fee_bps": SWAP_FEE_BPS}}


def trending() -> dict:
    data = listing(50)
    return {"board": data["tokens"], "count": data["count"],
            "promotions": active_promotions(),
            "ranked_by": RANKED_BY,
            "updated_at": int(time.time())}


# ---------------------------------------------------------------- promotions
def _fee_after_promo(code: str, quantity: float, promo: dict | None) -> int:
    gross = pricing.micro(code, quantity)
    discount = max(0, min(100, int((promo or {}).get("discount_percent") or 0)))
    return gross - gross * discount // 100


def promote(*, symbol: object, kind: object = "trending", days: object = 1,
            wallet: str = "founder", promo_code: object = "") -> dict:
    """Buy a trending slot (5 TOR/day) or the 7-day banner (30 TOR)."""
    ticker = _symbol(symbol)
    _token_or_404(ticker)
    style = str(kind or "trending").strip().lower()
    if style not in ("trending", "banner"):
        raise ValidationError("kind must be 'trending' or 'banner'")
    if style == "trending":
        try:
            length = int(days or 1)
        except (TypeError, ValueError):
            raise ValidationError("days must be a whole number")
        if length < 1 or length > MAX_DAY_PROMOTION:
            raise ValidationError(f"days must be between 1 and {MAX_DAY_PROMOTION}")
        code, quantity = "meme_trending_day", length
    else:
        length, code, quantity = 7, "meme_banner_7d", 1
    promo = find_promo(promo_code)
    charge = _charge(str(wallet or "founder"), code, quantity=quantity,
                     note=f"{ticker} {style}", promo=promo)
    now = int(time.time())
    row = PROMOTIONS.add({
        "symbol": ticker, "kind": style, "days": length, "status": "active",
        "starts_unix": now, "ends_unix": now + length * 86_400,
        "wallet": str(wallet or "founder"),
        "unit_price_tor": pricing.price(code),
        "paid_tor": charge.get("amount_tor"), "txid": charge.get("txid"),
        "waived": bool(charge.get("waived")),
        "promo_code": (promo or {}).get("code"),
        "created_at": _now(), "created_unix": now,
    })
    return {"promotion": row, "charge": charge,
            "price_tor": pricing.price(code) * quantity,
            "ends_at": datetime.fromtimestamp(row["ends_unix"], timezone.utc)
                              .isoformat(timespec="seconds")}


def promotions(limit: int = 50) -> dict:
    rows = active_promotions()[:max(1, int(limit))]
    return {"promotions": rows, "count": len(rows),
            "prices": {"trending_day_tor": pricing.price("meme_trending_day"),
                       "banner_7d_tor": pricing.price("meme_banner_7d")}}


# ------------------------------------------------------------ volume sessions
def volume_session(*, symbol: object, wallet: str = "founder",
                   rounds: object = 10, promo_code: object = "") -> dict:
    """Pay for a 15-minute volume session and queue it honestly."""
    ticker = _symbol(symbol)
    _token_or_404(ticker)
    try:
        count = int(rounds or 10)
    except (TypeError, ValueError):
        raise ValidationError("rounds must be a whole number")
    if count < 1 or count > 500:
        raise ValidationError("rounds must be between 1 and 500")
    promo = find_promo(promo_code)
    charge = _charge(str(wallet or "founder"), "meme_volume_session",
                     note=f"{ticker} volume session", promo=promo)
    now = int(time.time())
    row = SESSIONS.add({
        "symbol": ticker, "wallet": str(wallet or "founder"), "rounds": count,
        "status": "queued", "engine": "not_deployed",
        "run_endpoint": "/api/meme/volume-sessions/{id}/run",
        "trades": [], "paid_tor": charge.get("amount_tor"),
        "txid": charge.get("txid"), "waived": bool(charge.get("waived")),
        "promo_code": (promo or {}).get("code"),
        "created_at": _now(), "created_unix": now,
    })
    run_path = f"/api/meme/volume-sessions/{row['id']}/run"
    row = SESSIONS.update(str(row["id"]), run_endpoint=run_path)
    return {"session": row, "charge": charge,
            "honest": "the session is recorded and paid, but the trading engine "
                      "is not deployed — no trades are placed until it ships"}


def list_sessions(limit: int = 25) -> dict:
    rows = SESSIONS.newest(limit)
    return {"sessions": rows, "count": len(SESSIONS.all()),
            "engine": "not_deployed",
            "note": "queued sessions stay queued; the run endpoint answers 501"}


def run_volume_session(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    raise EngineNotDeployed(
        "the volume/market bot engine is not deployed on this node yet — the "
        "session stays queued and no trades were placed",
        details={"session": session, "engine": "not_deployed"})


# ------------------------------------------------------------- landing pages
def launch_landing(*, symbol: object, wallet: str = "founder",
                   prompt: object = "", model: str | None = None,
                   promo_code: object = "") -> dict:
    """Generate + host a token landing page with the local Ollama model."""
    ticker = _symbol(symbol)
    token = _token_or_404(ticker)
    record = _record(ticker) or {}
    _pool, view = _pool_view(ticker)
    supply = int(token.get("supply") or 0)
    brief = str(prompt or "").strip() or (
        f"Build a bold meme-coin landing page for {token.get('name') or ticker} "
        f"(${ticker}) on the OneNess TOR chain. Supply: {supply:,} tokens. "
        f"Description: {record.get('description') or 'a community coin on TOR'}. "
        f"Liquidity: {'live pool on the TOR AMM' if view else 'pool not launched'}. "
        f"Include a hero with the ticker, tokenomics, a roadmap, a 'how to buy on "
        f"TOR' section (create a wallet, buy from the pool) and a community footer. "
        f"No price promises and no financial advice.")
    promo = find_promo(promo_code)
    needed = _fee_after_promo("meme_landing_page", 1, promo)
    account = ledger.balance_of(str(wallet or "founder"))
    balance = int(account.get("balance_micro") or 0)
    if balance < needed:
        raise FeeError(
            f"{wallet} holds {format_tor(balance)} TOR but a landing page costs "
            f"{format_tor(needed)} TOR",
            details={"wallet": wallet, "balance_micro": balance,
                     "required_micro": needed, "code": "meme_landing_page"})
    generated = builder.generate(brief, title=f"${ticker}", model=model)
    charge = _charge(str(wallet or "founder"), "meme_landing_page",
                     note=f"{ticker} landing", promo=promo)
    site = builder.create(brief, generated["html"], model=generated["model"],
                          wallet=str(wallet or "founder"),
                          txid=charge.get("txid"), title=f"${ticker} landing",
                          charge=charge)
    if record:
        TOKENS.update(str(record.get("id")), landing_site=site["id"],
                      landing_url=site.get("preview_url"))
    return {"site": site, "charge": charge, "txid": charge.get("txid"),
            "model": generated["model"], "bytes": generated["bytes"],
            "symbol": ticker}


# ------------------------------------------------------------------ DEX seams
VENUES = [
    {"id": "oneness-amm", "name": "OneNess TOR AMM", "kind": "internal pool",
     "available": True, "status": "live", "fee": "0.30%",
     "endpoints": {"quote": "meme.quote", "swap": "meme.swap"},
     "note": "the pool the chain itself settles"},
    {"id": "raydium-cpmm", "name": "Raydium CP-Swap", "kind": "solana",
     "available": False, "status": "DEX_NOT_DEPLOYED", "fee": "0.25%",
     "endpoints": {"quote": "meme.dex_quote", "swap": "meme.dex_swap"},
     "note": "waits for the TOR↔Solana bridge and a bridged pool"},
    {"id": "jupiter", "name": "Jupiter aggregator", "kind": "solana",
     "available": False, "status": "DEX_NOT_DEPLOYED", "fee": "route",
     "endpoints": {"quote": "meme.dex_quote", "swap": "meme.dex_swap"},
     "note": "needs a Solana-side mint and a route"},
    {"id": "dexscreener", "name": "DexScreener index", "kind": "indexer",
     "available": False, "status": "DEX_NOT_DEPLOYED", "fee": "none",
     "endpoints": {}, "note": "indexing starts once a pool is public"},
]


def venues() -> dict:
    return {"venues": VENUES, "count": len(VENUES),
            "available": sum(1 for row in VENUES if row["available"]),
            "placeholders": sum(1 for row in VENUES if not row["available"]),
            "note": "Every unavailable venue answers DEX_NOT_DEPLOYED; the live "
                    "pool routes above are the ones that actually settle."}


def dex_quote(symbol: object = "", side: object = "buy",
              amount: object = None) -> dict:
    ticker = str(symbol or "").strip().upper()
    raise DexNotDeployed(
        "no external DEX is deployed for TOR yet — this endpoint is the seam for "
        "the bridge-era pools", details={
            "requested": {"symbol": ticker, "side": side, "amount": amount},
            "venues": [row["id"] for row in VENUES if not row["available"]],
            "live_alternative": {
                "meme.quote": "GET /api/meme/quote — real constant-product "
                              "quote from the live TOR pool"}})


def dex_swap(symbol: object = "", side: object = "buy",
             amount: object = None, wallet: str = "founder") -> dict:
    ticker = str(symbol or "").strip().upper()
    raise DexNotDeployed(
        "external DEX swaps are not deployed; nothing was signed and nothing was "
        "charged", details={
            "requested": {"symbol": ticker, "side": side, "amount": amount,
                          "wallet": wallet},
            "live_alternative": {
                "meme.swap": "POST /api/meme/pools/{symbol}/swap — real on-chain "
                             "swap against the TOR pool"}})


# ------------------------------------------------------------- DEX link parser
_ADDR = re.compile(r"[1-9A-HJ-NP-Za-km-z]{33,44}")
_HINT_PARAMS = ("token", "tokenaddress", "mint", "outputmint", "inputmint",
                "address", "coin", "contract", "pair", "pool")
_SKIP_PARAMS = ("maker", "maker2", "wallet", "owner", "ref", "referrer",
                "inviter", "aff")
_SOURCES = (("dexscreener", "dexscreener"), ("birdeye", "birdeye"),
            ("axiom", "axiom"), ("pump.fun", "pumpfun"), ("raydium", "raydium"),
            ("jup", "jupiter"), ("gmgn", "gmgn"), ("solscan", "solscan"),
            ("explorer", "explorer"), ("geckoterminal", "geckoterminal"))
_LABELS = {"dexscreener": "DexScreener", "birdeye": "Birdeye", "axiom": "Axiom",
           "pumpfun": "pump.fun", "raydium": "Raydium", "jupiter": "Jupiter",
           "gmgn": "GMGN", "solscan": "Solscan", "explorer": "Solana Explorer",
           "geckoterminal": "GeckoTerminal", "other": "DEX link"}


def _shaped(value: str) -> str | None:
    match = _ADDR.fullmatch((value or "").strip())
    return match.group(0) if match else None


def _found(value: str) -> str | None:
    match = _ADDR.search(value or "")
    return match.group(0) if match else None


def parse_dex_link(url: object) -> dict:
    """Classify any pasted DEX link → source, mint, pair (pure, never raises)."""
    raw = str(url or "").strip()
    empty = {"ok": False, "source": "", "label": "", "mint": None,
             "pair": None, "kind": None}
    bare = _shaped(raw)
    if bare:
        return {"ok": True, "source": "other", "label": _LABELS["other"],
                "mint": bare, "pair": None, "kind": "token"}
    if not raw:
        return dict(empty)
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlparse(raw)
        query = parse_qs(parsed.query or "", keep_blank_values=False)
    except ValueError:
        return dict(empty)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return dict(empty)
    source = next((key for needle, key in _SOURCES if needle in host), "other")
    out = {"ok": False, "source": source,
           "label": _LABELS.get(source, _LABELS["other"]),
           "mint": None, "pair": None, "kind": None}
    segments = [part for part in re.split(r"[/?#]+", parsed.path or "") if part]
    shaped = [seg for seg in (_shaped(part) for part in segments) if seg]

    def hint() -> str | None:
        for name in _HINT_PARAMS:
            for value in query.get(name, []):
                found = _found(value)
                if found:
                    return found
        for name, values in query.items():
            lowered = name.lower()
            if lowered in _SKIP_PARAMS or lowered.startswith(("utm_", "ref_")):
                continue
            for value in values:
                found = _found(value)
                if found:
                    return found
        return None

    if source in ("dexscreener", "geckoterminal"):
        if "token" in segments:
            index = segments.index("token")
            after = segments[index + 1:index + 2]
            out["mint"] = _shaped(after[0]) if after else None
        if out["mint"] is None and shaped:
            out["pair"] = shaped[0]
    else:
        out["mint"] = hint() or next(
            (addr for addr in (_found(seg) for seg in segments) if addr), None)
    if out["mint"] or out["pair"]:
        out["ok"] = True
        out["kind"] = "token" if out["mint"] else "pair"
    return out

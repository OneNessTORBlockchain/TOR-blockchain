"""USDC-quoted swap books — the platform's second pool family.

The Rust ledger models exactly one pool shape: ``TOR ⟷ token`` (``tor_reserve``
/ ``token_reserve``). The swap platform also sells USDC pairs, so those books
live here: this module owns each book's reserves, quotes the **same**
constant-product math the chain applies to TOR pools (0.30% to the book), and
settles every fill with two real signed transfers —

1. the trader's token in → the custody wallet,
2. the custody wallet's token out → the trader,

both broadcast through :mod:`oneness_tor.services.ledger`, so every fill is in
``/api/chain/events`` like any other transfer. What the chain does *not* do is
compute the USDC price: that is the book's own reserve ratio, reported as such.
Nothing here is simulated — a swap that cannot settle raises ``FeeError``.

Reserves are integers in token units / whole USDC units, matching the chain's
integer accounting.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.config import settings
from core.errors import FeeError, NotFound, ValidationError
from oneness_tor.services import ledger
from oneness_tor.services.store import Collection, data_dir

STANDARD = "oneness-tor-pool-book/1"

BOOKS = Collection(lambda: data_dir("market") / "usdc-pools.json", "book")

SWAP_FEE_BPS = 30
DEFAULT_USDC = "USDC"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def usdc_symbol() -> str:
    return str(settings.get("market.usdc_token") or DEFAULT_USDC).strip().upper()


def custody_wallet() -> str:
    """The one wallet that custodies every USDC book's reserves."""
    return str(settings.get("market.usdc_custody") or "swap-usdc-pool")


def _symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise ValidationError("symbol is required")
    return text


def _address(name: str) -> str:
    wallet = ledger.load_wallet(name)
    address = str(wallet.get("address") or "")
    if not address:
        raise ValidationError(f"wallet {name!r} has no address")
    return address


def _row(symbol: str) -> dict | None:
    for book in BOOKS.all():
        if str(book.get("symbol") or "").upper() == symbol:
            return book
    return None


def _view(book: dict) -> dict:
    tokens = int(book.get("token_reserve") or 0)
    usdc = int(book.get("usdc_reserve") or 0)
    price = (usdc / tokens) if tokens else None
    return {
        "base": usdc_symbol(),
        "symbol": str(book.get("symbol") or "").upper(),
        "token_reserve": tokens,
        "usdc_reserve": usdc,
        "fee_bps": int(book.get("fee_bps") or SWAP_FEE_BPS),
        "price_usd": (round(price, 12) if price else None),
        "liquidity_usd": round(usdc * 2.0, 2),
        "custody": str(book.get("custody") or ""),
        "created_at": book.get("created_at"),
        "seed": book.get("seed") or None,
        "chain": "two signed transfers per fill (book math on the node)",
    }


def catalog() -> list[dict]:
    """Every USDC book, deepest first."""
    rows = [_view(book) for book in BOOKS.all()]
    rows.sort(key=lambda row: row["liquidity_usd"], reverse=True)
    return rows


def get(symbol: object) -> dict:
    """One book's live view (raises NotFound)."""
    ticker = _symbol(symbol)
    book = _row(ticker)
    if not book:
        raise NotFound(f"no USDC pool for {ticker}",
                       details={"known": [row["symbol"] for row in catalog()][:40]})
    return _view(book)


def quote(symbol: object, side: object = "buy", amount: object = None) -> dict:
    """Constant-product quote in the book's own units.

    ``buy``: ``amount`` is USDC in, the answer is token units out.
    ``sell``: ``amount`` is token units in, the answer is USDC out.
    """
    view = get(symbol)
    direction = str(side or "buy").strip().lower()
    if direction not in ("buy", "sell"):
        raise ValidationError("side must be buy or sell")
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValidationError("amount must be a number")
    if value <= 0:
        raise ValidationError("amount must be greater than zero")
    fee = value * SWAP_FEE_BPS / 10_000.0
    net = value - fee
    tokens = view["token_reserve"]
    usdc = view["usdc_reserve"]
    if tokens <= 0 or usdc <= 0:
        raise FeeError("this book has no reserves — it cannot quote")
    if direction == "buy":
        out = (tokens * net) / (usdc + net)
        if out <= 0 or out >= tokens:
            raise ValidationError("amount too large for this book")
    else:
        out = (usdc * net) / (tokens + net)
        if out <= 0 or out >= usdc:
            raise ValidationError("amount too large for this book")
    return {"symbol": view["symbol"], "base": view["base"], "side": direction,
            "amount_in": round(value, 8), "unit_in": (view["base"] if direction == "buy"
                                                      else view["symbol"]),
            "amount_out": round(out, 8), "unit_out": (view["symbol"] if direction == "buy"
                                                      else view["base"]),
            "fee": round(fee, 8), "fee_bps": SWAP_FEE_BPS,
            "price_usd": view["price_usd"], "liquidity_usd": view["liquidity_usd"],
            "engine": "constant product on the book's reserves (node)"}


def _apply(symbol: str, *, tokens: int, usdc: int) -> None:
    book = _row(symbol) or {}
    BOOKS.update(str(book.get("id")), token_reserve=int(tokens),
                 usdc_reserve=int(usdc), updated_at=_now())


def swap(symbol: object, side: object = "buy", amount: object = None,
         wallet: str = "") -> dict:
    """Settle a fill with two real signed transfers and move the reserves."""
    preview = quote(symbol, side, amount)
    ticker = preview["symbol"]
    base = preview["base"]
    trader = str(wallet or "").strip()
    if not trader:
        raise ValidationError("wallet is required — a swap is paid by its own wallet")
    custody = custody_wallet()
    if trader == custody:
        raise ValidationError("the custody wallet cannot trade its own book")
    trader_address = _address(trader)
    custody_address = _address(custody)
    amount_in = int(round(float(preview["amount_in"])))
    amount_out = int(round(float(preview["amount_out"])))
    if amount_in <= 0 or amount_out <= 0:
        raise ValidationError("the amount is too small to settle (rounds to zero)")
    book = _row(ticker) or {}
    held = int(book.get("token_reserve") or 0)
    usdc_held = int(book.get("usdc_reserve") or 0)
    if preview["side"] == "buy":
        first = ledger.send_micro(trader, custody_address, amount_in, token=base,
                                  note=f"book buy {ticker}", platform_fee=False)
        second = ledger.send_micro(custody, trader_address, amount_out, token=ticker,
                                   note=f"book deliver {ticker}", platform_fee=False)
        usdc_held += amount_in
        held -= amount_out
    else:
        first = ledger.send_micro(trader, custody_address, amount_in, token=ticker,
                                  note=f"book sell {ticker}", platform_fee=False)
        second = ledger.send_micro(custody, trader_address, amount_out, token=base,
                                   note=f"book deliver {base}", platform_fee=False)
        held += amount_in
        usdc_held -= amount_out
    _apply(ticker, tokens=held, usdc=usdc_held)
    return {"swap": {"symbol": ticker, "side": preview["side"],
                     "amount_in": preview["amount_in"], "unit_in": preview["unit_in"],
                     "expected_out": preview["amount_out"],
                     "unit_out": preview["unit_out"]},
            "quote": preview, "pool": get(ticker), "wallet": trader,
            "address": trader_address, "txid": first.get("txid"),
            "deliver_txid": second.get("txid"),
            "height": second.get("height") or first.get("height"),
            "settlement": "two signed transfers on the TOR ledger"}


def register(*, symbol: object, token_reserve: object, usdc_reserve: object,
             seed: str = "", note: str = "") -> dict:
    """Record a book whose reserves were already moved into custody.

    The seeding tool transfers the reserves on chain first, then calls this, so
    a book never claims liquidity the custody wallet does not hold.
    """
    ticker = _symbol(symbol)
    try:
        tokens = int(float(token_reserve))
        usdc = int(float(usdc_reserve))
    except (TypeError, ValueError):
        raise ValidationError("reserves must be whole numbers")
    if tokens <= 0 or usdc <= 0:
        raise ValidationError("both reserves must be greater than zero")
    existing = _row(ticker)
    payload = {"symbol": ticker, "base": usdc_symbol(),
               "token_reserve": tokens, "usdc_reserve": usdc,
               "fee_bps": SWAP_FEE_BPS, "custody": custody_wallet(),
               "seed": seed or "manual", "note": str(note or "")[:200],
               "created_at": (existing or {}).get("created_at") or _now(),
               "updated_at": _now()}
    if existing:
        BOOKS.update(str(existing.get("id")), **payload)
    else:
        BOOKS.add(payload)
    return get(ticker)


def custody_balances() -> dict:
    """What the custody wallet actually holds on chain (the books' backing)."""
    try:
        account = ledger.node_account(_address(custody_wallet()))
    except Exception as problem:  # noqa: BLE001 - a missing wallet reads as empty
        return {"wallet": custody_wallet(), "error": str(problem), "tokens": {}}
    return {"wallet": custody_wallet(), "address": account.get("address"),
            "tor": int(account.get("balance_micro") or 0) / 1e8,
            "usdc": int((account.get("tokens") or {}).get(usdc_symbol()) or 0),
            "tokens": dict(account.get("tokens") or {})}


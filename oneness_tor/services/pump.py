"""TOR Pump — free mints, platform-seeded liquidity and a $25 000 curve.

What is real here:

* the mint is a chain ``token_create``; the 5 TOR the chain charges is **funded
  to the creator's wallet by the platform first**, so a launch costs the creator
  nothing (the transfer is in ``/api/chain/events``);
* the starting pool is a chain ``pool_create`` signed by the platform wallet
  (``pump-owner``) — the platform owns the pool, exactly as advertised, and the
  liquidity is real TOR pulled from the platform's own balance;
* the creator's first buy is a genuine ``swap`` with the creator's own TOR;
* the bonding curve is the pool's own market cap: it graduates at $25 000, and
  because the pool exists from block one the token is already on TOR-Screener
  and DarkWeb Crypto (they read the same ``/api/market/pools``).

Creator fees are the creator's share (20%) of the pool's 0.30% swap fee,
computed from **real 24 h volume** and paid out as a signed TOR transfer from
``pump-owner`` — a claim you can verify on chain.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.config import settings
from core.errors import FeeError, NotFound, ValidationError
from core.validator import format_tor, to_micro
from oneness_tor.services import ledger, market_engine, meme
from oneness_tor.services.store import Collection, data_dir

STANDARD = "oneness-tor-pump/1"
LAUNCHES = Collection(lambda: data_dir("market") / "pump-launches.json", "pump")

TARGET_MCAP_USD = 25_000.0
START_MCAP_USD = 12_500.0
PLATFORM_LIQUIDITY_USD = 5_000.0
CREATOR_FEE_SHARE = 0.20          # of the 0.30% pool fee
PLATFORM_WALLET = "pump-owner"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def platform_wallet() -> str:
    return str(settings.get("market.pump_wallet") or PLATFORM_WALLET)


def _tor_usd() -> float:
    return float(market_engine.snapshot()["tor"]["price_usd"] or 3.6)


def _address(name: str) -> str:
    wallet = ledger.load_wallet(name)
    address = str(wallet.get("address") or "")
    if not address:
        raise ValidationError(f"wallet {name!r} has no address")
    return address


def _launch(symbol: str) -> dict | None:
    for row in LAUNCHES.all():
        if str(row.get("symbol") or "").upper() == symbol:
            return row
    return None


def _fund(wallet: str, needed_micro: int) -> dict:
    """Top a wallet up from the platform so the launch itself stays free."""
    platform = platform_wallet()
    account = ledger.node_account(_address(wallet))
    held = int(account.get("balance_micro") or 0)
    shortfall = int(needed_micro) - held
    if shortfall <= 0:
        return {"funded": False, "held_micro": held}
    sent = ledger.send_micro(platform, _address(wallet), shortfall,
                             note="pump launch grant", platform_fee=False)
    return {"funded": True, "amount_micro": shortfall, "txid": sent.get("txid"),
            "held_micro": held}


def launch(*, symbol: object, name: object = "", description: object = "",
           image: object = "", wallet: str = "", supply: object = None,
           first_buy_tor: object = 0) -> dict:
    """Mint free, seed the platform's $5 000 pool, then let the creator buy in."""
    creator = str(wallet or "").strip()
    if not creator:
        raise ValidationError("wallet is required — a launch is owned by its creator")
    platform = platform_wallet()
    if creator == platform:
        raise ValidationError("launch from your own wallet, not the platform's")
    ticker = str(symbol or "").strip().upper()
    try:
        total = int(float(supply or 1_000_000_000))
    except (TypeError, ValueError):
        raise ValidationError("supply must be a whole number of tokens")
    if total < 1_000_000:
        raise ValidationError("supply must be at least 1,000,000 tokens")
    tor_usd = _tor_usd()
    grant = _fund(creator, to_micro(6.0))
    minted = meme.create_token(symbol=ticker, name=name, supply=total,
                               description=description, image=image, wallet=creator)
    # ---- platform liquidity: the platform buys the pool's token side, then
    #      signs pool_create itself (10 TOR) with real TOR from its own balance
    price_usd = START_MCAP_USD / total
    price_tor = price_usd / tor_usd
    liquidity_tor = PLATFORM_LIQUIDITY_USD / tor_usd
    tokens_in = int(min(total * 0.80, round(liquidity_tor / price_tor)))
    platform_address = _address(platform)
    moved = ledger.send_micro(creator, platform_address, tokens_in, token=ticker,
                              note="pump platform liquidity", platform_fee=False)
    _fund(platform, to_micro(liquidity_tor + 10.5))
    pool = meme.create_pool(symbol=ticker, token_amount=tokens_in,
                            tor_amount=liquidity_tor, wallet=platform)
    first_buy = None
    buy = float(first_buy_tor or 0)
    if buy > 0:
        _fund(creator, to_micro(buy + 0.2))
        first_buy = meme.swap(symbol=ticker, side="buy", amount=buy, wallet=creator)
    market_engine.snapshot(refresh=True)      # the new pool is in the engine now
    row = LAUNCHES.add({
        "symbol": ticker, "name": str(name or ticker)[:80],
        "creator": creator, "creator_address": _address(creator),
        "platform": platform, "platform_address": platform_address,
        "supply": total, "start_mcap_usd": START_MCAP_USD,
        "target_mcap_usd": TARGET_MCAP_USD,
        "liquidity_usd": PLATFORM_LIQUIDITY_USD,
        "price_usd_at_launch": round(price_usd, 12),
        "tokens_in_pool": tokens_in, "tor_in_pool": round(liquidity_tor, 8),
        "creator_fee_share": CREATOR_FEE_SHARE,
        "creator_fees_claimed_micro": 0,
        "mint_txid": minted.get("txid"), "pool_txid": pool.get("txid"),
        "liquidity_txid": moved.get("txid"),
        "first_buy_tor": buy, "first_buy": bool(first_buy),
        "grant": grant, "created_at": _now(), "standard": STANDARD,
    })
    return {"launch": row, "token": minted.get("token") or minted,
            "pool": pool, "first_buy": first_buy,
            "listing": "TOR-Screener and DarkWeb Crypto read /api/market/pools, "
                       "so this pool is listed the moment it exists",
            "curve": status(ticker)}



def status(symbol: object) -> dict:
    """The curve: market cap now, progress to $25 000, graduated or not."""
    ticker = str(symbol or "").strip().upper()
    row = _launch(ticker)
    if not row:
        raise NotFound(f"{ticker} was not launched on TOR Pump",
                       details={"known": [item["symbol"] for item in LAUNCHES.all()][:40]})
    market = market_engine.token(ticker)
    mcap = float(market.get("market_cap_usd") or 0)
    target = float(row.get("target_mcap_usd") or TARGET_MCAP_USD)
    return {**row,
            "price_usd": market.get("price_usd"),
            "price_tor": market.get("price_tor"),
            "market_cap_usd": market.get("market_cap_usd"),
            "liquidity_usd": market.get("liquidity_usd"),
            "volume_24h_usd": market.get("volume_24h_usd"),
            "change_24h": market.get("change_24h"),
            "progress": round(min(1.0, mcap / target) if target else 0.0, 6),
            "graduated": bool(target and mcap >= target),
            "creator_fees_due_micro": _fees_due(row, market),
            "curve": "constant product on the platform-owned pool; graduates at $25 000"}


def _fees_due(row: dict, market: dict) -> int:
    """Creator's 20% of the 0.30% pool fee over real 24 h volume, minus claims."""
    volume_tor = float(market.get("volume_24h_tor") or 0)
    accrued = volume_tor * 0.003 * CREATOR_FEE_SHARE
    claimed = int(row.get("creator_fees_claimed_micro") or 0) / 1e8
    return max(0, int(round((accrued - claimed) * 1e8)))


def list_launches(limit: int = 50) -> dict:
    """Every launch, newest first, with its live curve."""
    rows = sorted(LAUNCHES.all(), key=lambda row: str(row.get("created_at") or ""),
                  reverse=True)[:max(1, int(limit))]
    out = []
    for row in rows:
        try:
            out.append(status(row.get("symbol")))
        except Exception:  # noqa: BLE001 - a delisted symbol still lists
            out.append(dict(row))
    return {"standard": STANDARD, "count": len(out), "launches": out,
            "target_mcap_usd": TARGET_MCAP_USD,
            "platform_liquidity_usd": PLATFORM_LIQUIDITY_USD,
            "platform_wallet": platform_wallet(),
            "creator_fee_share": CREATOR_FEE_SHARE,
            "mint_fee": "free — the platform funds the chain's 5 TOR to your wallet"}


def claim_creator_fees(symbol: object, wallet: str) -> dict:
    """Pay the creator's accrued share as a real TOR transfer from the platform."""
    ticker = str(symbol or "").strip().upper()
    row = _launch(ticker)
    if not row:
        raise NotFound(f"{ticker} was not launched on TOR Pump")
    if row.get("creator") != str(wallet or "").strip():
        raise ValidationError("only the launch creator can claim these fees",
                              details={"creator": row.get("creator")})
    market = market_engine.token(ticker)
    due = _fees_due(row, market)
    if due <= 0:
        return {"claimed": False, "due_micro": 0, "symbol": ticker,
                "reason": "no accrued fees yet (they come from real pool volume)"}
    account = ledger.node_account(_address(platform_wallet()))
    if int(account.get("balance_micro") or 0) < due:
        raise FeeError("the platform wallet cannot cover this claim right now",
                       details={"due_micro": due})
    sent = ledger.send_micro(platform_wallet(), _address(str(wallet)), due,
                             note=f"creator fees {ticker}", platform_fee=False)
    LAUNCHES.update(str(row.get("id")),
                    creator_fees_claimed_micro=int(row.get("creator_fees_claimed_micro") or 0) + due,
                    last_claim_at=_now())
    return {"claimed": True, "symbol": ticker, "amount_micro": due,
            "amount_tor": format_tor(due), "txid": sent.get("txid"),
            "height": sent.get("height"), "wallet": str(wallet)}


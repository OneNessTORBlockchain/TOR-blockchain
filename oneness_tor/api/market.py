"""Market, pool, project, banner, AI gateway and pump endpoints.

One blueprint for the six products that ride on this node:

* ``/api/market/*``  — the price & market-cap engine (TOR at $3.60, every pool)
* ``/api/projects``  — the platform directory with logos and URLs
* ``/api/ads``       — banner rotation (permanent 1Ness banner + paid slots)
* ``/api/ai/*``      — OpenRouter free models and the AI-NFT ChatBox
* ``/api/pump/*``    — free mints, platform liquidity, the $25k bonding curve

Reads are public. Everything that moves value (a swap, a banner, a launch) is
paid by the caller's own wallet session or the node admin, exactly like
``/api/meme``.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import Forbidden, ValidationError
from oneness_tor.services import (ai_gateway, market_engine, meme, pool_books,
                                  projects, pump, wallet_access)
from oneness_tor.services.auth import is_admin_request

bp = Blueprint("market_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return payload


def _wallet(payload: dict) -> str:
    """The wallet that pays and owns: the session wallet, or an admin's choice."""
    held = wallet_access.session(request)
    wallet = str(payload.get("wallet") or (held or {}).get("wallet") or "")
    if not wallet and is_admin_request(request):
        wallet = "founder"
    if not wallet:
        raise Forbidden(
            "sign in at /wallet first — this action is paid and owned by your own "
            "account wallet", details={"header": wallet_access.HEADER})
    wallet_access.authorize_wallet(request, wallet)
    return wallet


# ------------------------------------------------------------------- engine
@bp.get("/market/overview")
def market_overview():
    """TOR, totals, movers, newest listings and the deepest pools."""
    return envelope(market_engine.overview())


@bp.get("/market/tokens")
def market_tokens():
    """Every token priced in USD, market cap and liquidity — newest first."""
    limit = request.args.get("limit", 500, type=int) or 500
    payload = market_engine.snapshot(max(1, min(2000, limit)))
    return envelope({"standard": payload["standard"], "at": payload["at"],
                     "tor": payload["tor"], "totals": payload["totals"],
                     "tokens": payload["tokens"], "sources": payload["sources"]})


@bp.get("/market/tokens/<symbol>")
def market_token(symbol: str):
    """One token: price, market cap, both pools, 24 h change."""
    return envelope(market_engine.token(symbol))


@bp.get("/market/chart/<symbol>")
def market_chart(symbol: str):
    """Candles generated from real sampled AMM prices (see the chart tool)."""
    points = request.args.get("points", type=int)
    return envelope(market_engine.chart(symbol, points))


@bp.get("/market/tor")
def market_tor():
    """TOR alone: the anchor, the drift applied and the resulting USD price."""
    return envelope(market_engine.snapshot()["tor"])


@bp.get("/market/pools")
def market_pools():
    """Every pool on the platform: TOR pools from the chain + USDC books."""
    base = request.args.get("base", "").strip().upper()
    limit = request.args.get("limit", 500, type=int) or 500
    payload = market_engine.snapshot(max(1, min(2000, limit)))
    rows = []
    for row in payload["tokens"]:
        for pool in row.get("pools_usd") or []:
            rows.append({"symbol": row["symbol"], "name": row.get("name"),
                         "base": pool["base"], "price_usd": pool.get("price_usd"),
                         "liquidity_usd": pool.get("liquidity_usd"),
                         "market_cap_usd": row.get("market_cap_usd"),
                         "image": row.get("image"),
                         "volume_24h_usd": row.get("volume_24h_usd"),
                         "change_24h": row.get("change_24h"),
                         "created_at": row.get("created_at")})
    if base:
        rows = [row for row in rows if row["base"] == base]
    rows.sort(key=lambda row: float(row.get("liquidity_usd") or 0), reverse=True)
    return envelope({"standard": "oneness-tor-pools/1", "count": len(rows),
                     "base": base or "all",
                     "tor_pools": payload["totals"]["tor_pools"],
                     "usdc_pools": payload["totals"]["usdc_pools"],
                     "liquidity_usd": payload["totals"]["liquidity_usd"],
                     "books": payload["books"], "pools": rows[:max(1, limit)]})


@bp.get("/market/quote")
def market_quote():
    """Quote a fill on whichever pool is deeper (USDC book or TOR pool)."""
    ticker = str(request.args.get("symbol") or "").strip().upper()
    side = request.args.get("side", "buy")
    amount = request.args.get("amount", type=float)
    base = request.args.get("base", "").strip().upper()
    row = market_engine.token(ticker)
    bases = [pool["base"] for pool in row.get("pools_usd") or []]
    if base == "USDC" or ("USDC" in bases and "TOR" not in bases):
        return envelope(pool_books.quote(ticker, side, amount))
    return envelope(meme.quote(ticker, side, amount))


@bp.post("/market/swap")
def market_swap():
    """Settle a fill: chain ``swap`` on a TOR pool, two transfers on a book."""
    payload = _body()
    wallet = _wallet(payload)
    ticker = str(payload.get("symbol") or "").strip().upper()
    base = str(payload.get("base") or "").strip().upper()
    row = market_engine.token(ticker)
    bases = [pool["base"] for pool in row.get("pools_usd") or []]
    if base == "USDC" or ("USDC" in bases and "TOR" not in bases):
        return envelope(pool_books.swap(ticker, payload.get("side", "buy"),
                                        payload.get("amount"), wallet=wallet))
    return envelope(meme.swap(symbol=ticker, side=payload.get("side", "buy"),
                              amount=payload.get("amount"), wallet=wallet))


# ----------------------------------------------------------------- projects
@bp.get("/projects")
def project_catalog():
    """The six TOR platforms: logo, folder, dev URL, onion and clearnet URLs."""
    return envelope(projects.catalog())


@bp.get("/ads")
def ad_rotation():
    """Banner rotation: the permanent 1Ness banner first, then paid slots."""
    limit = request.args.get("limit", 6, type=int) or 6
    return envelope(projects.banners(limit))


@bp.post("/ads")
def ad_buy():
    """Buy a 7-day banner slot for 50 TOR (paid from the caller's wallet)."""
    payload = _body()
    wallet = _wallet(payload)
    ticker = payload.get("symbol")
    if not ticker:
        raise ValidationError("symbol is required — the banner advertises a token")
    result = meme.promote(symbol=ticker, kind="banner", wallet=wallet,
                          promo_code=payload.get("promo_code", ""))
    meme_titles = {key: str(payload.get(key) or "").strip()[:120]
                   for key in ("title", "subtitle", "image", "link")}
    if any(meme_titles.values()):
        meme.PROMOTIONS.update(str(result["promotion"]["id"]), **meme_titles)
    return envelope({**result, "banner": projects.banners()})


# ----------------------------------------------------------------------- AI
@bp.get("/ai/models")
def ai_models():
    """Every free model the ChatBox dropdown can offer (refreshed on demand)."""
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    return envelope(ai_gateway.models(refresh=refresh))


@bp.get("/ai/status")
def ai_status():
    """Gateway readiness — never the key."""
    return envelope(ai_gateway.status())


@bp.post("/ai/chat")
def ai_chat():
    """The AI-NFT ChatBox: free OpenRouter model, TOR persona, no key in the browser."""
    payload = _body()
    return envelope(ai_gateway.chat(
        prompt=payload.get("prompt") or "",
        messages=payload.get("messages"),
        model=payload.get("model") or "", system=payload.get("system") or "",
        nft=payload.get("nft") if isinstance(payload.get("nft"), dict) else None,
        temperature=payload.get("temperature") or 0.6,
        max_tokens=payload.get("max_tokens") or 700))


# --------------------------------------------------------------------- pump
@bp.get("/pump/launches")
def pump_launches():
    """Every TOR Pump launch with its live bonding curve."""
    limit = request.args.get("limit", 50, type=int) or 50
    return envelope(pump.list_launches(limit))


@bp.get("/pump/launches/<symbol>")
def pump_launch(symbol: str):
    """One launch: pool, market cap, progress to $25 000, fees due."""
    return envelope(pump.status(symbol))


@bp.post("/pump/launch")
def pump_launch_new():
    """Mint free, platform seeds $5 000 of liquidity, creator buys first."""
    payload = _body()
    wallet = _wallet(payload)
    return envelope(pump.launch(
        symbol=payload.get("symbol"), name=payload.get("name", ""),
        description=payload.get("description", ""), image=payload.get("image", ""),
        wallet=wallet, supply=payload.get("supply"),
        first_buy_tor=payload.get("first_buy_tor") or 0))


@bp.post("/pump/launches/<symbol>/claim")
def pump_claim(symbol: str):
    """Claim the creator's share of the pool fees (a real TOR transfer)."""
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    wallet = _wallet(payload)
    return envelope(pump.claim_creator_fees(symbol, wallet))

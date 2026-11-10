"""Meme coin launcher API — real on-chain tokens, pools, swaps and paid tools."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import Forbidden, ValidationError
from oneness_tor.services import meme, wallet_access
from oneness_tor.services.auth import is_admin_request

bp = Blueprint("meme_api", __name__, url_prefix="/api/meme")


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
    """The wallet that pays/owns: the visitor's session wallet, or an admin's choice."""
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


# --------------------------------------------------------------------- tools
@bp.get("/tools")
def meme_tools():
    return envelope(meme.tools())


@bp.get("/promo")
def meme_promo():
    code = request.args.get("code")
    catalog = meme.promo_catalog()
    if not code:
        return envelope({**catalog, "valid": None})
    match = meme.find_promo(code)
    return envelope({**catalog, "valid": bool(match), "code": str(code).upper(),
                     "discount_percent": (match or {}).get("discount_percent", 0)})


# -------------------------------------------------------------------- tokens
@bp.get("/tokens")
def meme_tokens():
    limit = request.args.get("limit", 50, type=int) or 50
    return envelope(meme.listing(limit))


@bp.get("/tokens/<symbol>")
def meme_token(symbol: str):
    return envelope(meme.token_status(symbol))


@bp.post("/tokens")
def meme_token_create():
    payload = _body()
    flag = payload.get("pin_metadata", True)
    pin_metadata = str(flag).strip().lower() not in ("0", "false", "no", "off")
    return envelope(meme.create_token(
        symbol=payload.get("symbol"), name=payload.get("name"),
        supply=payload.get("supply"), description=payload.get("description", ""),
        image=payload.get("image", ""), wallet=_wallet(payload),
        socials=payload.get("socials"), promo_code=payload.get("promo_code", ""),
        pin_metadata=pin_metadata))


@bp.get("/tokens/<symbol>/market")
def meme_market(symbol: str):
    return envelope(meme.market(symbol))


@bp.post("/tokens/<symbol>/landing")
def meme_landing(symbol: str):
    payload = _body()
    return envelope(meme.launch_landing(
        symbol=symbol, wallet=_wallet(payload),
        prompt=payload.get("prompt", ""), model=payload.get("model"),
        promo_code=payload.get("promo_code", "")))


# --------------------------------------------------------------------- pools
@bp.post("/pools")
def meme_pool_create():
    payload = _body()
    return envelope(meme.create_pool(
        symbol=payload.get("symbol"), token_amount=payload.get("token_amount"),
        tor_amount=payload.get("tor_amount"), wallet=_wallet(payload)))


@bp.post("/pools/<symbol>/swap")
def meme_swap(symbol: str):
    payload = _body()
    return envelope(meme.swap(symbol=symbol, side=payload.get("side", "buy"),
                              amount=payload.get("amount"), wallet=_wallet(payload)))


@bp.get("/quote")
def meme_quote():
    symbol = request.args.get("symbol")
    if not symbol:
        raise ValidationError("symbol is required")
    amount = request.args.get("amount")
    return envelope(meme.quote(symbol, request.args.get("side", "buy"), amount))


# ---------------------------------------------------------------- promotions
@bp.get("/trending")
def meme_trending():
    return envelope(meme.trending())


@bp.get("/promotions")
def meme_promotions():
    limit = request.args.get("limit", 50, type=int) or 50
    return envelope(meme.promotions(limit))


@bp.post("/promotions")
def meme_promote():
    payload = _body()
    return envelope(meme.promote(
        symbol=payload.get("symbol"), kind=payload.get("kind", "trending"),
        days=payload.get("days", 1), wallet=_wallet(payload),
        promo_code=payload.get("promo_code", "")))


# ------------------------------------------------------------ volume sessions
@bp.get("/volume-sessions")
def meme_volume_sessions():
    limit = request.args.get("limit", 25, type=int) or 25
    return envelope(meme.list_sessions(limit))


@bp.post("/volume-sessions")
def meme_volume_session_create():
    payload = _body()
    return envelope(meme.volume_session(
        symbol=payload.get("symbol"), wallet=_wallet(payload),
        rounds=payload.get("rounds", 10), promo_code=payload.get("promo_code", "")))


@bp.post("/volume-sessions/<session_id>/run")
def meme_volume_session_run(session_id: str):
    session = meme.SESSIONS.get(session_id)
    wallet = str(session.get("wallet") or "")
    if is_admin_request(request):
        pass
    elif wallet:
        wallet_access.authorize_wallet(request, wallet)
    else:
        raise Forbidden("this session has no owner wallet — admin required")
    return envelope(meme.run_volume_session(session_id))


# ------------------------------------------------------------------ DEX seams
@bp.get("/dex/venues")
def meme_dex_venues():
    return envelope(meme.venues())


@bp.get("/dex/quote")
def meme_dex_quote():
    return envelope(meme.dex_quote(request.args.get("symbol", ""),
                                   request.args.get("side", "buy"),
                                   request.args.get("amount")))


@bp.post("/dex/swap")
def meme_dex_swap():
    payload = _body()
    return envelope(meme.dex_swap(payload.get("symbol", ""),
                                  payload.get("side", "buy"),
                                  payload.get("amount"), _wallet(payload)))


@bp.get("/dex/link")
def meme_dex_link():
    url = request.args.get("url", "")
    return envelope({**meme.parse_dex_link(url), "url": str(url)[:300]})

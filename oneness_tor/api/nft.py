"""AI-NFT Studio API — catalogue, 3 TOR mint, metadata load and the NFT chat."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import nft as nft_meta
from core.errors import Forbidden, ValidationError
from core.tools import ToolBox
from oneness_tor.services import base_url, nft_studio, wallet_access
from oneness_tor.services.auth import require_admin, require_member, token_from_request

bp = Blueprint("nft_api", __name__, url_prefix="/api/nft")


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


def _toolbox() -> ToolBox:
    # loopback, not request.host_url: an onion Host is unresolvable in-process
    return ToolBox(base_url.internal_base(),
                   display_base=base_url.request_base(request),
                   admin_token=token_from_request(request))


@bp.get("/catalogue")
def nft_catalogue():
    limit = request.args.get("limit", 100, type=int) or 100
    return envelope(nft_studio.catalogue(limit))


@bp.get("/catalogue/<nft_id>")
def nft_get(nft_id: str):
    return envelope(nft_studio.get_nft(nft_id))


@bp.post("/mint")
def nft_mint():
    payload = _body()
    held = wallet_access.session(request)
    wallet = str(payload.get("wallet") or (held or {}).get("wallet") or "")
    if not wallet:
        raise Forbidden(
            "sign in at /wallet first — the 3 TOR mint is paid and owned by your own "
            "account wallet", details={"header": wallet_access.HEADER})
    wallet_access.authorize_wallet(request, wallet)
    return envelope(nft_studio.mint(
        name=payload.get("name"), description=payload.get("description", ""),
        image=payload.get("image", ""), wallet=wallet,
        symbol=payload.get("symbol", "NFT"), attributes=payload.get("attributes"),
        animation_url=payload.get("animation_url", ""),
        external_url=payload.get("external_url", "")))


@bp.get("/metadata")
def nft_metadata():
    address = request.args.get("address", "")
    token_id = request.args.get("token_id")
    metadata_uri = request.args.get("metadata_uri", "")
    nft = nft_studio.load(address, token_id, metadata_uri)
    return envelope({"nft": nft, "prompt": nft_meta.persona_block(
        nft, nft.get("token_id"))})


@bp.post("/chat")
def nft_chat():
    require_member(request)
    payload = _body()
    prompt = str(payload.get("prompt") or "").strip()
    messages = payload.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise ValidationError("messages must be a list of {role, content}")
    if not prompt and not messages:
        raise ValidationError("prompt (or messages) is required")
    return envelope(nft_studio.chat(
        payload.get("address"), payload.get("token_id"), prompt or None,
        model=payload.get("model"), messages=messages, toolbox=_toolbox()))

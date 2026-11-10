"""IPFS/Pinata surface: what the node pins, and where the gateway is.

    GET  /api/ipfs/status   credential presence, gateway, last pin (public)
    POST /api/ipfs/pin      pin a JSON document / base64 image (admin)
    GET  /api/ipfs/resolve  ipfs:// or ar:// → HTTPS gateway URL (public)

Meme-token launch metadata and AI-NFT metadata are pinned automatically; this
blueprint exposes the same primitive so an operator can pin an extra document
(collection metadata, a marketplace manifest) without touching the stores.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import ipfs
from core.errors import ValidationError
from oneness_tor.services.auth import require_admin

bp = Blueprint("ipfs_api", __name__, url_prefix="/api")


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


@bp.get("/ipfs/status")
def ipfs_status():
    return envelope(ipfs.status())


@bp.get("/ipfs/resolve")
def ipfs_resolve():
    uri = request.args.get("uri", "")
    if not str(uri).strip():
        raise ValidationError("uri is required, e.g. ipfs://<cid>/meta.json")
    return envelope({"uri": str(uri), "url": ipfs.resolve(uri),
                     "cid": ipfs.cid_of(uri), "gateway": ipfs.gateway(),
                     "gateways": ipfs.gateways()})


@bp.post("/ipfs/pin")
def ipfs_pin():
    require_admin(request)
    payload = _body()
    name = str(payload.get("name") or "metadata.json")
    if payload.get("data_url"):
        result = ipfs.pin_data_url(payload["data_url"],
                                   filename=str(payload.get("filename") or "image"))
    elif isinstance(payload.get("document", payload.get("json")), dict):
        result = ipfs.pin_json(payload.get("document", payload.get("json")), name=name)
    else:
        raise ValidationError(
            "send {document: {...}} for JSON, or {data_url, filename} for an image")
    if not result.get("pinned"):
        raise ValidationError(f"pin failed: {result.get('error') or 'unknown error'}",
                              details=result)
    return envelope(result)

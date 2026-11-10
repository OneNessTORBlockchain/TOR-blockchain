"""Developer API: buy keys in TOR, then call the versioned API with one.

    GET    /api/dev/keys/pricing   price, limits, scopes (public)
    POST   /api/dev/keys           buy a key from your wallet (session)
    GET    /api/dev/keys           keys of this account, masked (session)
    DELETE /api/dev/keys/<id>      revoke one (session)
    GET    /api/v1/<endpoint>      call a public endpoint (x-api-key)
    POST   /api/v1/<endpoint>      same, with a JSON body (x-api-key)

``/api/v1/<endpoint>`` uses the exact catalog names (``chain.info``,
``tokens.list``, …) and forwards to the real route inside this process, so a
developer gets the same envelope the website gets — no duplicated logic. The
key is the only credential forwarded: spending still needs the wallet owner.
"""
from __future__ import annotations

import urllib.parse

from core import api_catalog
from core.errors import Forbidden, NotFound, ValidationError
from flask import Blueprint, current_app, jsonify, request

from oneness_tor.services import developer_keys, wallet_access

bp = Blueprint("dev_api", __name__, url_prefix="/api")


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


def _session_wallet() -> str:
    held = wallet_access.session(request)
    if not held:
        raise Forbidden(
            "sign in at /wallet first — a developer key is bought from your own "
            "wallet", details={"header": wallet_access.HEADER})
    return str(held.get("wallet") or "")


@bp.get("/dev/keys/pricing")
def dev_pricing():
    """The price and the limits — safe to show before anyone signs in."""
    return envelope({"pricing": developer_keys.pricing_doc(),
                     "how": ["sign in at /wallet",
                             "POST /api/dev/keys {label, days}",
                             "send x-api-key to /api/v1/<endpoint>"],
                     "catalog": "/api/docs"})


@bp.get("/dev/keys")
def dev_list():
    return envelope(developer_keys.list_for(_session_wallet()))


@bp.post("/dev/keys")
def dev_create():
    wallet = _session_wallet()
    payload = _body()
    wallet_access.authorize_wallet(request,
                                   str(payload.get("wallet") or wallet))
    response = envelope(developer_keys.issue(
        str(payload.get("wallet") or wallet), label=payload.get("label", ""),
        days=payload.get("days")))
    response.headers["Cache-Control"] = "no-store, no-cache, private"
    return response


@bp.delete("/dev/keys/<key_id>")
def dev_revoke(key_id: str):
    return envelope(developer_keys.revoke(_session_wallet(), key_id))


@bp.route("/v1/<endpoint>", methods=["GET", "POST"])
def dev_call(endpoint: str):
    """The paid developer surface: a real request, a real response, one key."""
    key = developer_keys.require(request)
    entry = api_catalog.by_name(endpoint)
    if entry is None:
        raise NotFound(f"unknown endpoint {endpoint!r}",
                       details={"catalog": "/api/docs/endpoints"})
    if entry["group"] == "dev" or entry["path"].startswith("/api/v1"):
        raise ValidationError("the developer gateway cannot call itself")

    query = {k: v for k, v in request.args.items() if k != "api_key"}
    body = request.get_json(silent=True) if request.method == "POST" else None
    body = dict(body) if isinstance(body, dict) else {}

    path = entry["path"]
    for param in entry["params"]:
        if param["in"] != "path":
            continue
        value = query.pop(param["name"], None)
        if value in (None, ""):
            value = body.pop(param["name"], None)
        if value in (None, ""):
            raise ValidationError(f"{param['name']} is required for {endpoint}",
                                  details={"endpoint": endpoint})
        path = path.replace("{" + param["name"] + "}",
                            urllib.parse.quote(str(value), safe=""))

    client = current_app.test_client()
    forwarded = {}
    presented = (request.headers.get(developer_keys.HEADER) or "").strip()
    if presented:
        forwarded[developer_keys.HEADER] = presented
    if request.method == "GET":
        response = client.get(path, query_string=query, headers=forwarded)
    else:
        if not body and query:
            body = dict(query)          # developers may post JSON params as query
            query = {}
        response = client.post(path, json=body, query_string=query,
                               headers=forwarded)

    developer_keys.spend(key, path, response.status_code)
    payload = response.get_json(silent=True)
    if isinstance(payload, dict):
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        meta.update({"developer_key": {"id": key.get("id"),
                                       "hint": key.get("hint"),
                                       "calls": int(key.get("calls") or 0)},
                     "request": {"method": request.method, "path": path},
                     "limits": developer_keys.limits()})
        payload["meta"] = meta
        return jsonify(payload), response.status_code
    return (response.get_data(as_text=True), response.status_code,
            {"content-type": response.headers.get("content-type",
                                                  "text/plain")})

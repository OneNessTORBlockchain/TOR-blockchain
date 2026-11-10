"""Admin fee schedule — the operator prices every service from the panel.

    GET    /api/admin/fees           the live table, its defaults and the history
    POST   /api/admin/fees          reprice: {"prices": {"<code>": <TOR>}}
    DELETE /api/admin/fees/<code>   reset one row to its published price
    DELETE /api/admin/fees          reset every override

Every call needs an admin credential — the same one ``/admin`` signs in with
(``x-admin-session``), the chain token (``x-admin-token``) or a rotated platform
key. A saved price is live for the next request: the fee router reads
``core.pricing``, never a copy, so the panel cannot drift from the invoice.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import ValidationError
from oneness_tor.services import service_fees
from oneness_tor.services.auth import require_admin

bp = Blueprint("fees_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _actor(request) -> str:
    """Who changed a price — recorded in the store's history."""
    held = str(require_admin(request))
    return "admin-session" if held == "session" else "admin-credential"


@bp.get("/admin/fees")
def fees_get():
    """The fee schedule: live price, published default and who set it."""
    require_admin(request)
    return envelope(service_fees.table())


@bp.post("/admin/fees")
def fees_set():
    """Reprice one or many services; LOCKED rows need ``confirm_locked``."""
    actor = _actor(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    items = payload.get("prices", payload.get("fees"))
    if items is None:
        items = {key: value for key, value in payload.items()
                 if key not in ("confirm_locked", "prices", "fees")}
    if not items:
        raise ValidationError('send {"prices": {"<code>": <TOR>}}',
                              details={"endpoint": "POST /api/admin/fees"})
    return envelope(service_fees.update(
        items, confirm_locked=bool(payload.get("confirm_locked")), actor=actor))


@bp.delete("/admin/fees/<code>")
def fees_reset_one(code: str):
    return envelope(service_fees.reset(code, actor=_actor(request)))


@bp.delete("/admin/fees")
def fees_reset_all():
    return envelope(service_fees.reset(None, actor=_actor(request)))

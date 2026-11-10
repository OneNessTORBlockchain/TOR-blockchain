"""Username + password accounts: wallet signup/login and the node admin.

    POST /api/auth/signup              real wallet + one-time private key
    POST /api/auth/login               wallet session for the logged-in wallet
    GET  /api/auth/me                  the signed-in wallet's own balance
    GET  /api/auth/wallets             up to 5 wallets of this account
    POST /api/auth/wallets             generate another wallet (password)
    POST /api/auth/wallets/import      import a private key / mnemonic (password)
    POST /api/auth/wallets/<name>/export   password-authorized key export
    POST /api/auth/wallets/<name>/select   move the session to that wallet
    POST /api/auth/wallets/<name>/primary  make it the default login wallet
    POST /api/auth/logout              drop the wallet session
    GET  /api/auth/support             the support email
    GET  /api/admin/account            is an admin registered? (public status)
    POST /api/admin/account/register   first run only; closed after that
    POST /api/admin/account/login      username + password → x-admin-session
    POST /api/admin/account/logout
    POST /api/admin/account/password   change password (closes old sessions)
    POST /api/admin/account/username   change username
    DELETE /api/admin/account          delete → registration re-opens
    POST /api/admin/account/api-key    rotate the platform API key (once)
    POST /api/admin/account/wallet     new wallet + fee collector (chain token)

The signup response and a password-authorized export are the only places a
private key ever appears. An account holds up to five wallets — generated here
or imported — and every one is listed by ``GET /api/auth/wallets``.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import Forbidden, ValidationError
from core.node_client import node
from oneness_tor.services import accounts, chain_token, ledger, wallet_access
from oneness_tor.services.auth import (is_admin_request, require_admin,
                                       require_chain_admin)

bp = Blueprint("accounts_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _secret_response(data, **meta):
    """A response carrying key material must never be cached by a browser/proxy."""
    response = envelope(data, **meta)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


def _body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return payload


# ------------------------------------------------------------------- wallets
@bp.post("/auth/signup")
def signup():
    payload = _body()
    return _secret_response(accounts.signup_user(payload.get("username"),
                                                 payload.get("password")))


@bp.post("/auth/login")
def login():
    payload = _body()
    return _secret_response(accounts.login_user(payload.get("username"),
                                                payload.get("password")))


@bp.get("/auth/me")
def me():
    held = wallet_access.session(request)
    if not held:
        raise Forbidden("wallet session required — POST /api/auth/login first",
                        details={"header": wallet_access.HEADER})
    name = str(held.get("wallet"))
    profile = accounts.user_profile(name, str(held.get("address") or ""))
    try:
        held_wallets = accounts.user_wallets(name)
    except Forbidden:
        # a bare signed session (no account row) still sees its own wallet
        held_wallets = {"wallets": [ledger.public_wallet(name)], "count": 1,
                        "max_wallets": accounts.MAX_WALLETS}
    rows = held_wallets.get("wallets") or []
    view = next((row for row in rows if row.get("name") == name), None) \
        or ledger.public_wallet(name)
    return envelope({**profile, "wallet": view,
                     "wallets": rows or [view],
                     "wallet_count": held_wallets.get("count") or 1,
                     "max_wallets": held_wallets.get("max_wallets") or 5,
                     "session": {"header": wallet_access.HEADER,
                                 "expires_at": held.get("expires_at")}})


@bp.post("/auth/logout")
def logout():
    return envelope(wallet_access.close_session(request))


def _held() -> dict:
    held = wallet_access.session(request)
    if not held:
        raise Forbidden("sign in at /wallet first — this needs a wallet session",
                        details={"header": wallet_access.HEADER})
    return held


@bp.get("/auth/wallets")
def auth_wallets():
    """Up to five wallets this account owns, with live public balances."""
    held = _held()
    return envelope(accounts.user_wallets(str(held.get("wallet"))))


@bp.post("/auth/wallets")
def auth_wallet_generate():
    """Generate another wallet (password-authorized; 5 per account)."""
    held = _held()
    payload = _body()
    return _secret_response(accounts.generate_user_wallet(
        str(held.get("wallet")), payload.get("password"),
        label=payload.get("label", ""), words=payload.get("words") or 24))


@bp.post("/auth/wallets/import")
def auth_wallet_import():
    """Import a Phantom private key (or a recovery phrase) into the account."""
    held = _held()
    payload = _body()
    if not str(payload.get("secret_key_base58") or "").strip() \
            and not str(payload.get("mnemonic") or "").strip():
        raise ValidationError(
            "send secret_key_base58 (Phantom export) or mnemonic (12-24 words)")
    return envelope(accounts.import_user_wallet(
        str(held.get("wallet")), payload.get("password"),
        secret_key_base58=payload.get("secret_key_base58"),
        mnemonic=payload.get("mnemonic"), label=payload.get("label", "")))


@bp.post("/auth/wallets/<name>/export")
def auth_wallet_export(name: str):
    """Password-authorized private key export — the only re-readable reveal."""
    held = _held()
    payload = _body()
    if not str(payload.get("password") or ""):
        raise ValidationError("the account password is required to export a key")
    return _secret_response(accounts.export_user_wallet(
        str(held.get("wallet")), payload.get("password"),
        target=payload.get("target") or name))


@bp.post("/auth/wallets/<name>/select")
def auth_wallet_select(name: str):
    """Move the session onto another wallet the same account owns."""
    held = _held()
    return envelope(accounts.select_user_wallet(str(held.get("wallet")), name))


@bp.post("/auth/wallets/<name>/primary")
def auth_wallet_primary(name: str):
    """Make another owned wallet the default that a login opens."""
    held = _held()
    return envelope(accounts.set_primary_user_wallet(str(held.get("wallet")), name))


@bp.get("/auth/support")
def support():
    return envelope({"email": accounts.support_email(),
                     "support_email": accounts.support_email(),
                     "note": "OneNess TOR support — accounts and wallets"})


# --------------------------------------------------------------------- admin
@bp.get("/admin/account")
def admin_status():
    return envelope(accounts.admin_status(include_private=is_admin_request(request)))


@bp.post("/admin/account/register")
def admin_register():
    payload = _body()
    return envelope(accounts.register_admin(payload.get("username"),
                                            payload.get("password"),
                                            payload.get("admin_token")))


@bp.post("/admin/account/login")
def admin_login():
    payload = _body()
    return envelope(accounts.login_admin(payload.get("username"),
                                         payload.get("password")))


@bp.post("/admin/account/logout")
def admin_logout():
    require_admin(request)
    return envelope(accounts.close_session(request))


@bp.post("/admin/account/password")
def admin_password():
    require_admin(request)
    payload = _body()
    return envelope(accounts.change_password(payload.get("current_password"),
                                             payload.get("new_password")))


@bp.post("/admin/account/username")
def admin_username():
    require_admin(request)
    payload = _body()
    return envelope(accounts.change_username(payload.get("password"),
                                             payload.get("username")
                                             or payload.get("new_username")))


@bp.delete("/admin/account")
def admin_delete():
    require_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(accounts.delete_admin(payload.get("password")))


@bp.post("/admin/account/api-key")
def admin_api_key():
    require_admin(request)
    return _secret_response(accounts.rotate_api_key())


@bp.get("/admin/token")
def admin_chain_token():
    """Where this node's chain token comes from — never the token itself."""
    require_admin(request)
    return envelope(chain_token.status())


@bp.post("/admin/account/wallet")
def admin_wallet():
    chain_token = require_chain_admin(request)
    return envelope(accounts.generate_admin_wallet(chain_token))


@bp.post("/admin/fee-receiver")
def admin_fee_receiver():
    """Point the node's platform/service fees at another wallet (chain admin)."""
    token = require_chain_admin(request)
    payload = _body()
    address = str(payload.get("address") or "").strip()
    if not address:
        raise ValidationError("address is required, e.g. TOR1…onion")
    reply = node.set_fee_receiver(address, token=token)
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise ValidationError(
            f"the node refused the fee receiver: {error.get('message', 'unknown')}",
            details={"node_error": error})
    return envelope(reply.get("data") or {})

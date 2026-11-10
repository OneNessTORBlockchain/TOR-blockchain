"""Admin authentication shared by every privileged route.

Three credentials are accepted, and all three mean "the operator":

1. **the chain admin token** — ``torchain init`` printed it once and stores only
   ``sha3-256(token)`` in ``state.json``; we verify a presented token by hashing
   it and comparing against the chain. Before genesis exists,
   ``api.admin_token`` from ``config.yaml`` is accepted so setup works.
2. **a platform API key** — minted/rotated from the admin account settings
   (``POST /api/admin/account/api-key``); only its sha3 hash is stored.
3. **an admin session** — ``POST /api/admin/account/login`` with the username and
   password registered at ``/admin``. Sent as ``x-admin-session``.

Chain-level actions (sealing the archive, moving the fee receiver) also need a
*chain* token. An admin session carries one when the account was registered with
it (encrypted at rest under the password); otherwise :func:`require_chain_admin`
uses the token this deployment keeps for itself in ``data/admin/chain_token``
(see :mod:`oneness_tor.services.chain_token`), so nobody has to paste a token
that ``torchain init`` printed once.
"""
from __future__ import annotations

import hashlib

from core.config import settings
from core.errors import Forbidden
from oneness_tor.services.store import read_json


def _accounts():
    """Late import — ``accounts`` itself uses this module for is_admin_request."""
    from oneness_tor.services import accounts

    return accounts


def _chain_token():
    """Late import — ``chain_token`` is a store, not an authenticator."""
    from oneness_tor.services import chain_token

    return chain_token


def expected_hash() -> str | None:
    state = read_json(settings.chain_dir / "state.json", {}) or {}
    value = state.get("admin_token_sha256")
    return str(value) if value else None


def token_from_request(request) -> str | None:
    """Header first (``x-admin-token`` / Bearer), then query, then JSON body."""
    header = request.headers.get("x-admin-token")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    token = request.args.get("token")
    if token:
        return token.strip()
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict) and body.get("admin_token"):
            return str(body["admin_token"]).strip()
    return None


def is_admin(token: str | None) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    digest = hashlib.sha3_256(text.encode("utf-8")).hexdigest()
    expected = expected_hash()
    if expected:
        return digest == expected.lower()
    return text == settings.admin_token


def admin_session(request) -> dict | None:
    """The live username/password admin session, if any."""
    return _accounts().session_from_request(request)


def is_admin_request(request) -> bool:
    """True for a chain token, a rotated platform key, or an admin session."""
    token = token_from_request(request)
    if is_admin(token):
        return True
    if _accounts().platform_key_matches(token):
        return True
    return admin_session(request) is not None


def require_admin(request) -> str:
    """Return the accepted credential or raise 403 with a fixable message."""
    token = token_from_request(request)
    if is_admin(token):
        return str(token)
    if _accounts().platform_key_matches(token):
        return str(token)
    if admin_session(request) is not None:
        return "session"
    if not expected_hash():
        raise Forbidden(
            "no chain admin token found (data/chain/state.json missing?) — run "
            "`torchain init --admin-token <token>` or set api.admin_token",
            details={"chain_dir": str(settings.chain_dir)})
    raise Forbidden(
        "admin credential required: sign in with the admin username/password "
        "(x-admin-session), send a rotated platform key, or send the chain token "
        "printed by `torchain init` in the x-admin-token header",
        details={"headers": ["x-admin-session", "x-admin-token"]})


def require_member(request) -> str:
    """A signed-in account: the operator's admin session, or any wallet session.

    The node's AI surfaces (command deck, AI-NFT chat, the fetch tool) are open to
    the public, but each call still belongs to somebody: sign up at ``/wallet`` and
    the request carries ``x-wallet-session``. No token is ever asked for.
    """
    if is_admin_request(request):
        return "admin"
    from oneness_tor.services import wallet_access

    held = wallet_access.session(request)
    if held:
        return "wallet"
    raise Forbidden(
        "sign in at /wallet first — this node's AI endpoints are open to every "
        "signed-in account (username + password)",
        details={"header": wallet_access.HEADER})


def require_chain_admin(request) -> str:
    """The *chain* admin token — header, the session's bound token, or this node's.

    A signed-in admin (or a rotated platform key) never has to type anything: the
    deployment keeps its own token in ``data/admin/chain_token``, the start
    command writes its sha3 into the ledger, and ``chain_token.usable()`` hands
    it back here. A header still wins when it is right, and an account that bound
    a token keeps using that one.
    """
    token = token_from_request(request)
    if is_admin(token):
        return str(token)
    session = admin_session(request)
    if session and str(session.get("chain_token") or "").strip():
        return str(session["chain_token"])
    if is_admin_request(request):
        held = _chain_token().usable()
        if held:
            return held
        raise Forbidden(
            "the running ledger holds an admin token this deployment cannot "
            "reproduce: restart the node (the start command adopts "
            "data/admin/chain_token on boot), or set ONENESS_ADMIN_TOKEN to the "
            "token `torchain init` printed",
            details={"header": "x-admin-token",
                     "token_file": str(_chain_token().store_path())})
    raise Forbidden(
        "admin credential required: sign in at /admin (x-admin-session) — that "
        "is enough for chain-level actions — or send the chain token in "
        "x-admin-token",
        details={"headers": ["x-admin-session", "x-admin-token"]})

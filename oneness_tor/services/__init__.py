"""Service layer: ledger access, payments, contracts, workbench, Tor.

The blueprints stay thin — they validate input, call one service function and
wrap the result in the envelope. Anything that touches the chain goes through
:mod:`oneness_tor.services.ledger`, which signs with the real Ed25519 wallet.
"""
from __future__ import annotations

from core.errors import AppError

__all__ = ["unwrap", "STATUS_FOR"]

STATUS_FOR = {
    "NOT_FOUND": 404,
    "UNKNOWN_TOKEN": 404,
    "POOL_NOT_FOUND": 404,
    "UNAUTHORIZED": 403,
    "FORBIDDEN": 403,
    "INSUFFICIENT_FUNDS": 402,
    "NONCE_MISMATCH": 409,
    "CONFLICT": 409,
    "VALIDATION_ERROR": 400,
    "BAD_JSON": 400,
}


def unwrap(reply: dict, *, default: str = "the TOR node rejected the request") -> dict:
    """Return ``reply['data']`` on success, raise the node's error otherwise.

    The node's ``code`` is preserved so the UI and the Ollama brain can react to
    ``INSUFFICIENT_FUNDS`` / ``NONCE_MISMATCH`` / … without parsing English.
    """
    if isinstance(reply, dict) and reply.get("ok"):
        return reply.get("data") or {}
    error = (reply or {}).get("error") or {}
    code = str(error.get("code") or "CHAIN_ERROR")
    message = str(error.get("message") or default)
    raise AppError(message, code=code,
                   http_status=int(error.get("http_status")
                                   or STATUS_FOR.get(code, 400)),
                   details=error.get("details") or {})

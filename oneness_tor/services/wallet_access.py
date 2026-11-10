"""Spending is private: prove you hold the wallet's key (or be the node admin).

Balances are **public** on this chain (see
:mod:`oneness_tor.services.public_ledger`), but everything that can *move* value
or reveal a private memo still needs proof: the receive panel, the transaction
history, signing and sending. That proof is either the operator's admin session
(``/admin``) or a short-lived session earned by the account login at ``/wallet``
(or by signing a challenge with the wallet's own Ed25519 key — no secret ever
travels):

1. ``POST /api/wallets/<name>/challenge`` → nonce + the exact message to sign,
2. the client signs it and posts ``{nonce, signature}`` to
   ``POST /api/wallets/<name>/session``,
3. the returned token goes in the ``x-wallet-session`` header for those reads.

Sessions live in this process only (short TTL, one wallet), which is exactly
what a per-wallet write view needs. No page asks the visitor for an admin token.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from datetime import datetime, timezone

import base58
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.errors import Forbidden, ValidationError
from oneness_tor.services import ledger
from oneness_tor.services.auth import is_admin_request

PREFIX = "oneness-tor:wallet-access:v1"
CHALLENGE_TTL = 300
SESSION_TTL = 3600
HEADER = "x-wallet-session"

_LOCK = threading.RLock()
_CHALLENGES: dict[str, dict] = {}
_SESSIONS: dict[str, dict] = {}


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(timespec="seconds")


def address_for_pubkey(pubkey: bytes) -> str:
    """``TOR1`` + base32(sha3-256(pubkey)[:20]) + ``onion`` — the chain's rule."""
    digest = hashlib.sha3_256(pubkey).digest()[:20]
    body = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return "TOR1" + body + "onion"


def message(address: str, nonce: str, issued: int) -> str:
    """The exact bytes the wallet must sign."""
    return f"{PREFIX}|{address}|{nonce}|{int(issued)}"


def _prune(now: int | None = None) -> None:
    stamp = int(now if now is not None else time.time())
    for store in (_CHALLENGES, _SESSIONS):
        for key, value in list(store.items()):
            if int(value.get("expires") or 0) <= stamp:
                store.pop(key, None)


def challenge(name: str) -> dict:
    """A single-use nonce bound to one wallet, valid for five minutes."""
    wallet = ledger.load_wallet(name)
    address = str(wallet.get("address") or "").strip()
    if not address:
        raise ValidationError(f"wallet {name!r} has no address",
                              details={"wallet": name})
    issued = int(time.time())
    expires = issued + CHALLENGE_TTL
    nonce = secrets.token_urlsafe(24)
    text = message(address, nonce, issued)
    with _LOCK:
        _prune(issued)
        _CHALLENGES[nonce] = {"wallet": str(name), "address": address,
                              "message": text, "expires": expires}
    return {"wallet": str(name), "address": address, "nonce": nonce,
            "message": text, "expires_at": _iso(expires),
            "ttl_seconds": CHALLENGE_TTL,
            "how": ("sign `message` with the wallet's Ed25519 key (base58), then "
                    f"POST /api/wallets/{name}/session with "
                    "{nonce, signature}")}


def verify(name: str, nonce: object, signature: object) -> dict:
    """Check the signature against the wallet's pubkey and mint a session."""
    text = str(nonce or "").strip()
    signed = str(signature or "").strip()
    if not text or not signed:
        raise ValidationError("nonce and signature are required")
    with _LOCK:
        _prune()
        held = _CHALLENGES.get(text)
        if not held:
            raise Forbidden(
                "that challenge is unknown or expired — request a new one from "
                f"POST /api/wallets/{name}/challenge")
        if held["wallet"] != str(name):
            raise Forbidden("this challenge belongs to another wallet")
        _CHALLENGES.pop(text, None)  # single use, even when the check fails
    wallet = ledger.load_wallet(name)
    try:
        public = base58.b58decode(str(wallet.get("pubkey_base58") or ""))
        raw_signature = base58.b58decode(signed)
    except ValueError as problem:
        raise ValidationError(f"signature must be base58: {problem}") from problem
    if address_for_pubkey(public) != held["address"]:
        raise Forbidden("the wallet file's address does not match its pubkey",
                        details={"wallet": str(name)})
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            raw_signature, held["message"].encode("utf-8"))
    except (InvalidSignature, ValueError):
        raise Forbidden("that signature does not belong to this wallet",
                        details={"wallet": str(name)})
    expires = int(time.time()) + SESSION_TTL
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {"wallet": str(name), "address": held["address"],
                            "expires": expires}
    return {"wallet": str(name), "address": held["address"], "session": token,
            "header": HEADER, "expires_at": _iso(expires),
            "ttl_seconds": SESSION_TTL}


def _session(request) -> dict | None:
    presented = (request.headers.get(HEADER) or request.args.get("wallet_session")
                 or "").strip()
    if not presented:
        return None
    with _LOCK:
        _prune()
        held = _SESSIONS.get(presented)
        return dict(held) if held else None


def wallet_for_address(address: str) -> str | None:
    """Which keystore wallet owns ``address`` (``None`` when none does)."""
    wanted = str(address or "").strip()
    if not wanted:
        return None
    for name in ledger.wallet_names():
        try:
            if str(ledger.load_wallet(name).get("address")) == wanted:
                return name
        except Exception:  # noqa: BLE001 - a broken key file must not 500
            continue
    return None


def open_session(name: str, address: str) -> dict:
    """Mint a session directly (used by password login, not by the challenge).

    The wallet key still never leaves the node: a username/password login only
    happens after the account's scrypt hash verified, so the caller already
    proved ownership of the account that owns this keystore.
    """
    address = str(address or "").strip()
    if not address:
        raise ValidationError(f"wallet {name!r} has no address",
                              details={"wallet": str(name)})
    expires = int(time.time()) + SESSION_TTL
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _prune()
        _SESSIONS[token] = {"wallet": str(name), "address": address,
                            "expires": expires}
    return {"wallet": str(name), "address": address, "session": token,
            "header": HEADER, "expires_at": _iso(expires),
            "ttl_seconds": SESSION_TTL}


def session(request) -> dict | None:
    """The live wallet session for this request (public helper)."""
    return _session(request)


def close_session(request) -> dict:
    """Drop the presented wallet session (logout)."""
    presented = (request.headers.get(HEADER) or request.args.get("wallet_session")
                 or "").strip()
    with _LOCK:
        removed = _SESSIONS.pop(presented, None) if presented else None
    return {"logged_out": bool(removed)}


def _denied(name: str | None, address: str | None = None) -> Forbidden:
    who = f"wallet {name}'s history and spending" if name else "this address"
    return Forbidden(
        f"{who} are private — sign in at /wallet with the account that owns it, "
        "or sign the challenge with its own Ed25519 key (balances themselves "
        "are public: GET /api/public/balances)",
        details={"challenge": (f"POST /api/wallets/{name}/challenge"
                               if name else None),
                 "address": address, "header": HEADER})


def _same_account(held: dict | None, name: str) -> bool:
    """True when a wallet session and ``name`` belong to one account (max 5)."""
    if not held or not name:
        return False
    if held.get("wallet") == str(name):
        return True
    try:
        from oneness_tor.services import accounts

        return accounts.wallet_belongs_to(name, held.get("wallet"))
    except Exception:  # noqa: BLE001 - an unknown wallet is simply not a sibling
        return False


def authorize(request, name: str) -> str:
    """``"admin"``/``"session"`` for a wallet-name read, else 403."""
    if is_admin_request(request):
        return "admin"
    held = _session(request)
    if _same_account(held, str(name)):
        return "session"
    raise _denied(str(name))


def authorize_wallet(request, name: str) -> str:
    """Alias used by paid actions: the wallet's owner, or any admin credential."""
    return authorize(request, name)


def authorize_address(request, address: str) -> str:
    """Same rule for an address read: admin, or any wallet of the owning account."""
    if is_admin_request(request):
        return "admin"
    held = _session(request)
    owner = wallet_for_address(address)
    if _same_account(held, owner or ""):
        return "session"
    raise _denied(owner, str(address))

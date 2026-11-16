"""Wallet ownership, proved to the node without ever sending a private key.

Protocol ``oneness-tor/wallet-proof/1`` -- the seam the three paid ANONYMOUS
products (Desktop, webapp, API) use to say "this TOR1...onion wallet is mine".
The chain is the only place the answer is decided: a product holds a keystore
file, the node holds nothing, and the proof travels as four headers:

    X-Oneness-Wallet      the ED25519 public key, base58
    X-Oneness-Timestamp   unix seconds, integer
    X-Oneness-Nonce       base58, >= 16 random bytes, single use
    X-Oneness-Signature   base58 signature over the canonical string

    canonical = method + "\\n" + path + "\\n" + timestamp + "\\n" + nonce
                + "\\n" + sha256(raw body) as lowercase hex

The order of the checks is the security property, so it is fixed:

1. a missing or malformed header is a 401, before anything else runs;
2. ``timestamp`` outside +/- ``SKEW_SECONDS`` is a 401 (clock skew, said plainly);
3. a nonce already seen inside ``NONCE_WINDOW`` is a 401 (a replay);
4. a signature that does not verify for that public key is a 401 -- **the header
   is never trusted without the signature**;
5. only then is the public key turned into its ``TOR1...onion`` address, and a
   caller that named an address in the path must match it (403).

Nothing here mints a session or a token: the proof lasts exactly as long as one
request, which is all a membership check needs.

The nonces live in this process (``_NONCES``), the same place
:mod:`oneness_tor.services.wallet_access` keeps its challenges and sessions. A
multi-process deployment would need them in the node's own cache; running two
workers without that would let a replay through the other worker, and that is
stated here rather than papered over.
"""
from __future__ import annotations

import hashlib
import threading
import time

import base58
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.errors import Forbidden, Unauthorized, ValidationError
from oneness_tor.services.wallet_access import address_for_pubkey

#: The document name every answer of this module carries.
STANDARD = "oneness-tor/wallet-proof/1"
WALLET_HEADER = "X-Oneness-Wallet"
TIMESTAMP_HEADER = "X-Oneness-Timestamp"
NONCE_HEADER = "X-Oneness-Nonce"
SIGNATURE_HEADER = "X-Oneness-Signature"
HEADERS = (WALLET_HEADER, TIMESTAMP_HEADER, NONCE_HEADER, SIGNATURE_HEADER)
#: A clock more than two minutes out is not a wallet, it is a broken client.
SKEW_SECONDS = 120
#: How long a nonce is remembered (one window, not forever: memory is finite).
NONCE_WINDOW = 300

_LOCK = threading.RLock()
_NONCES: dict[str, int] = {}


def body_hash(raw: bytes | str | None = None) -> str:
    """sha256 of the request body, lowercase hex (``""`` and ``None`` alike)."""
    if raw is None:
        return hashlib.sha256(b"").hexdigest()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(bytes(raw)).hexdigest()


def canonical(method: str, path: str, timestamp: object, nonce: object,
              digest: object) -> str:
    """The exact string a wallet signs -- identical in every product."""
    return (f"{str(method or '').upper()}\n{str(path or '')}\n"
            f"{int(timestamp)}\n{str(nonce or '').strip()}\n{str(digest or '')}")


def _public_key(text: object) -> bytes:
    raw = base58.b58decode(str(text or "").strip())
    if len(raw) != 32:
        raise ValidationError("the wallet public key must decode to 32 bytes")
    return raw


def _signature(text: object) -> bytes:
    raw = base58.b58decode(str(text or "").strip())
    if len(raw) != 64:
        raise ValidationError("the signature must decode to 64 bytes")
    return raw


def verify_signature(pubkey_base58: object, message: bytes | str,
                     signature_base58: object) -> bool:
    """True when ``signature`` is that public key's signature over ``message``."""
    try:
        public = _public_key(pubkey_base58)
        signed = _signature(signature_base58)
    except (ValueError, ValidationError):
        return False
    body = message.encode("utf-8") if isinstance(message, str) else bytes(message)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signed, body)
    except (InvalidSignature, ValueError):
        return False
    return True


def _remember(nonce: str, moment: int) -> None:
    """Keep one nonce for ``NONCE_WINDOW`` seconds; the second use is refused."""
    with _LOCK:
        for key, expires in list(_NONCES.items()):
            if expires <= moment:
                _NONCES.pop(key, None)
        if nonce in _NONCES:
            raise Unauthorized(
                "that nonce has already been used - every signed request needs a "
                "fresh one",
                details={"nonce": nonce, "window_seconds": NONCE_WINDOW})
        _NONCES[nonce] = moment + NONCE_WINDOW


def reset() -> None:
    """Forget every nonce (tests, and a deliberate restart of the node)."""
    with _LOCK:
        _NONCES.clear()


def prove(*, headers, method: str, path: str, raw_body: bytes | str | None,
          now: int | None = None) -> dict:
    """Check the four headers for one request; return who proved it.

    ``headers`` is any mapping with case-insensitive lookup (a Flask
    ``request.headers`` works, so does a plain dict). Raises
    :class:`Unauthorized` (401) or :class:`Forbidden` (403) and returns
    ``{"address", "pubkey_base58", "timestamp", "nonce", "message"}`` otherwise.
    """
    held: dict[str, str] = {}

    def _get(name: str) -> str:
        for candidate in (name, name.lower()):
            try:
                value = headers.get(candidate)
            except AttributeError:      # not a mapping at all
                value = None
            if value:
                return str(value).strip()
        return ""

    for name in HEADERS:
        held[name] = _get(name)
    missing = [name for name in HEADERS if not held[name]]
    if missing:
        raise Unauthorized(
            "this call needs the wallet's own signature: send "
            + ", ".join(HEADERS),
            details={"missing": missing, "standard": STANDARD})

    try:
        stamp = int(held[TIMESTAMP_HEADER])
    except (TypeError, ValueError):
        raise Unauthorized("X-Oneness-Timestamp must be whole unix seconds",
                           details={"received": held[TIMESTAMP_HEADER]})
    moment = int(now if now is not None else time.time())
    if abs(moment - stamp) > SKEW_SECONDS:
        raise Unauthorized(
            f"that timestamp is {abs(moment - stamp)}s away from this node's clock "
            f"- the allowed skew is {SKEW_SECONDS}s",
            details={"timestamp": stamp, "node_time": moment,
                     "skew_seconds": SKEW_SECONDS})

    try:
        nonce_raw = base58.b58decode(held[NONCE_HEADER])
    except ValueError:
        raise Unauthorized("X-Oneness-Nonce must be base58",
                           details={"received": held[NONCE_HEADER][:80]})
    if len(nonce_raw) < 16:
        raise Unauthorized("X-Oneness-Nonce must be at least 16 random bytes",
                           details={"bytes": len(nonce_raw)})

    message = canonical(method, path, stamp, held[NONCE_HEADER],
                        body_hash(raw_body))
    if not verify_signature(held[WALLET_HEADER], message,
                            held[SIGNATURE_HEADER]):
        raise Unauthorized(
            "that signature does not belong to this public key (or the body, "
            "method or path it covered is not the one received)",
            details={"standard": STANDARD, "signed": message})

    _remember(held[NONCE_HEADER], moment)
    public = _public_key(held[WALLET_HEADER])
    return {"standard": STANDARD, "address": address_for_pubkey(public),
            "pubkey_base58": held[WALLET_HEADER], "timestamp": stamp,
            "nonce": held[NONCE_HEADER], "message": message}


def require(request, *, address: object = None) -> dict:
    """Flask helper: prove this request, then check it is the wallet it names.

    ``address`` is the wallet in the path (``/membership/<address>/...``). A
    request signed by another wallet is a 403 -- the proof belongs to the caller,
    so one wallet can never act on another wallet's subscription.
    """
    proved = prove(headers=request.headers, method=request.method,
                   path=request.path, raw_body=request.get_data(cache=True))
    wanted = str(address or "").strip()
    if wanted and wanted != proved["address"]:
        raise Forbidden(
            "this request was signed by a different wallet than the one in the path",
            details={"signed_by": proved["address"], "path_wallet": wanted})
    return proved

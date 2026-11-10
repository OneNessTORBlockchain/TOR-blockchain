"""Username + password accounts — wallet signup and the node admin.

Two account kinds live in one store (``data/accounts/accounts.json``):

* **user** — created by ``POST /api/auth/signup``. The signup generates a real
  Phantom-compatible Ed25519 wallet (``torchain wallet new``), shows its private
  key **once**, and keeps only the public fields + a scrypt password hash. The
  wallet session returned by login is the same signed-session mechanism the
  wallet page already used (``x-wallet-session``), so every paid action
  (mints, AI-NFTs, deployments) is charged to and owned by that wallet.
* **admin** — exactly one, created by the operator at ``/admin`` with a
  username and a password. Deleting it re-opens registration. Settings cover
  password/username change, API-key rotation and generating a fresh admin
  wallet that becomes the chain's fee collector.

Nothing secret is stored in the clear: passwords are scrypt hashes, API keys are
sha3 hashes, and the optional chain admin token (needed for chain-level actions
such as sealing the archive or moving the fee collector) is encrypted with
AES-GCM under a key derived from the password. Sessions are in-process and
short-lived; the decrypted chain token only ever lives inside a session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import settings
from core.errors import AppError, Forbidden, NotFound, ValidationError
from core.node_client import node
from oneness_tor.services import ledger, wallet_access
from oneness_tor.services.store import Collection, data_dir, read_json

ACCOUNTS = Collection(lambda: data_dir("accounts") / "accounts.json", "acct")

ADMIN_HEADER = "x-admin-session"
API_KEY_HEADER = "x-admin-token"          # platform keys ride the same header
ADMIN_SESSION_TTL = 7200
SUPPORT_EMAIL_DEFAULT = "support@onenesstech.ca"
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
RESERVED = {"admin", "administrator", "root", "support", "system", "founder",
            "oneness", "wallet", "node", "genesis"}
PASSWORD_MIN = 8
#: One account may hold at most this many wallets (generated + imported).
MAX_WALLETS = 5
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
#: Key-derivation cost for the encrypted chain token (deliberately heavier).
SEAL_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
TOKEN_AAD = b"oneness-tor:admin-chain-token:v1"

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}
#: Wrong-password attempts per account wallet: at most ten in five minutes.
_FAILS: dict[str, list[float]] = {}
FAIL_LIMIT = 10
FAIL_WINDOW = 300


# --------------------------------------------------------------------- helpers
def _iso(stamp: int | None = None) -> str:
    moment = stamp if stamp is not None else int(time.time())
    return datetime.fromtimestamp(int(moment), timezone.utc).isoformat(timespec="seconds")


def _scrypt(password: str, salt: bytes, *, n: int, r: int, p: int,
            dklen: int = 32) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r),
                          p=int(p), dklen=int(dklen), maxmem=64 * 1024 * 1024)


def hash_password(password: object) -> dict:
    """scrypt hash — the plaintext never touches disk."""
    text = str(password or "")
    if len(text) < PASSWORD_MIN:
        raise ValidationError(
            f"password must be at least {PASSWORD_MIN} characters",
            details={"min_length": PASSWORD_MIN})
    salt = os.urandom(16)
    digest = _scrypt(text, salt, **SCRYPT)
    return {"algo": "scrypt", **SCRYPT, "salt": salt.hex(), "hash": digest.hex()}


def check_password(password: object, stored: object) -> bool:
    row = stored if isinstance(stored, dict) else {}
    try:
        salt = bytes.fromhex(str(row.get("salt") or ""))
        expected = bytes.fromhex(str(row.get("hash") or ""))
        if not salt or not expected:
            return False
        digest = _scrypt(str(password or ""), salt, n=int(row.get("n") or SCRYPT["n"]),
                         r=int(row.get("r") or SCRYPT["r"]),
                         p=int(row.get("p") or SCRYPT["p"]),
                         dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _seal(password: str, value: str) -> dict:
    """Encrypt the chain admin token under a password-derived AES-GCM key."""
    salt = os.urandom(16)
    key = _scrypt(password, salt, **SEAL_SCRYPT)
    nonce = os.urandom(12)
    box = AESGCM(key).encrypt(nonce, value.encode("utf-8"), TOKEN_AAD)
    return {"salt": salt.hex(), "nonce": nonce.hex(),
            "box": base64.b64encode(box).decode("ascii"), "kdf": "scrypt-aesgcm"}


def _unseal(password: str, sealed: object) -> str | None:
    row = sealed if isinstance(sealed, dict) else {}
    try:
        salt = bytes.fromhex(str(row.get("salt") or ""))
        nonce = bytes.fromhex(str(row.get("nonce") or ""))
        box = base64.b64decode(str(row.get("box") or ""))
        key = _scrypt(password, salt, **SEAL_SCRYPT)
        return AESGCM(key).decrypt(nonce, box, TOKEN_AAD).decode("utf-8")
    except (ValueError, TypeError, InvalidTag):
        return None


def username_key(value: object) -> str:
    return str(value or "").strip().lower()


def clean_username(value: object) -> str:
    text = str(value or "").strip()
    if not USERNAME.match(text):
        raise ValidationError(
            "username must be 3-32 characters: letters, digits, dot, dash or "
            "underscore, starting with a letter or digit",
            details={"received": text[:64]})
    if text.lower() in RESERVED:
        raise ValidationError(f"username {text!r} is reserved",
                              details={"reserved": sorted(RESERVED)})
    return text


def support_email() -> str:
    return str(settings.get("support.email") or SUPPORT_EMAIL_DEFAULT)


def _wallet_name(prefix: str, username: str) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "-", username.lower()).strip("-")[:40] or "user"
    candidate = f"{prefix}{base}"[:47]
    taken = set(ledger.wallet_names())
    if candidate not in taken:
        return candidate
    for index in range(2, 100):
        option = f"{prefix}{base}"[:44] + f"-{index}"
        if option not in taken:
            return option
    raise ValidationError("could not allocate a wallet name for that username")


def account_wallets(row: dict | None) -> list[str]:
    """Every wallet name this account owns, primary first (max ``MAX_WALLETS``).

    Older rows only carry ``wallet``; the primary is folded into the list so the
    migration needs no rewrite and no account can lose its wallet.
    """
    row = row or {}
    stored = row.get("wallets")
    names = [str(item).strip() for item in stored
             if str(item).strip()] if isinstance(stored, list) else []
    primary = str(row.get("wallet") or "").strip()
    if primary and primary not in names:
        names.insert(0, primary)
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique[:MAX_WALLETS]


def wallet_meta(row: dict | None) -> dict:
    """``{wallet_name: {origin, label, added_at}}`` — cosmetic, never secret."""
    stored = (row or {}).get("wallet_meta")
    return {str(key): dict(value) for key, value in stored.items()
            if isinstance(value, dict)} if isinstance(stored, dict) else {}


def wallet_belongs_to(wallet_name: object, owner_wallet: object) -> bool:
    """True when ``wallet_name`` is one of ``owner_wallet``'s account wallets."""
    target = str(wallet_name or "").strip()
    owner = str(owner_wallet or "").strip()
    if not target or not owner:
        return False
    row = user_for_wallet(owner)
    return bool(row) and target in account_wallets(row)


def _wallet_slot_name(username: str, index: int) -> str:
    """A free keystore name for the account's ``index``-th wallet."""
    base = re.sub(r"[^a-z0-9._-]+", "-", str(username or "user").lower()).strip("-")
    base = (base or "user")[:32]
    taken = set(ledger.wallet_names())
    for bump in range(int(index), int(index) + 100):
        candidate = f"u_{base}-{bump}"[:47]
        if candidate not in taken:
            return candidate
    raise ValidationError("could not allocate a wallet name for this account")


def _public(row: dict) -> dict:
    wallets = account_wallets(row)
    return {"id": row.get("id"), "role": row.get("role"),
            "username": row.get("username"), "wallet": row.get("wallet"),
            "address": row.get("address"), "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"), "wallets": wallets,
            "wallet_count": len(wallets), "max_wallets": MAX_WALLETS,
            "wallet_meta": wallet_meta(row)}


def _find(role: str) -> dict | None:
    for row in ACCOUNTS.all():
        if str(row.get("role")) == role:
            return row
    return None


def find_username(username: object) -> dict | None:
    wanted = username_key(username)
    return next((row for row in ACCOUNTS.all()
                 if username_key(row.get("username")) == wanted), None)


# ------------------------------------------------------------------- sessions
def _prune(now: int | None = None) -> None:
    stamp = int(now if now is not None else time.time())
    with _LOCK:
        for token, held in list(_SESSIONS.items()):
            if int(held.get("expires") or 0) <= stamp:
                _SESSIONS.pop(token, None)


def _open(row: dict, password: str) -> dict:
    expires = int(time.time()) + ADMIN_SESSION_TTL
    token = secrets.token_urlsafe(32)
    session = {"session": token, "header": ADMIN_HEADER,
               "role": row.get("role"), "username": row.get("username"),
               "account_id": row.get("id"), "wallet": row.get("wallet"),
               "address": row.get("address"), "expires": expires,
               "expires_at": _iso(expires), "ttl_seconds": ADMIN_SESSION_TTL,
               "chain_token": _unseal(password, row.get("chain_token"))}
    with _LOCK:
        _prune()
        _SESSIONS[token] = dict(session)
    return {"session": token, "header": ADMIN_HEADER, "role": row.get("role"),
            "username": row.get("username"), "wallet": row.get("wallet"),
            "address": row.get("address"), "expires_at": session["expires_at"],
            "ttl_seconds": ADMIN_SESSION_TTL,
            "chain_token_bound": bool(session["chain_token"])}


def session_from_request(request) -> dict | None:
    """The live admin session for this request (header or ``?admin_session=``)."""
    presented = (request.headers.get(ADMIN_HEADER)
                 or request.args.get("admin_session") or "").strip()
    if not presented:
        return None
    with _LOCK:
        _prune()
        held = _SESSIONS.get(presented)
        return dict(held) if held else None


def close_session(request) -> dict:
    presented = (request.headers.get(ADMIN_HEADER)
                 or request.args.get("admin_session") or "").strip()
    with _LOCK:
        removed = _SESSIONS.pop(presented, None) if presented else None
    return {"logged_out": bool(removed)}


def clear_sessions(username: str | None = None) -> int:
    with _LOCK:
        doomed = [token for token, held in _SESSIONS.items()
                  if username is None or held.get("username") == username]
        for token in doomed:
            _SESSIONS.pop(token, None)
    return len(doomed)


# ---------------------------------------------------------------------- admin
def admin_account() -> dict | None:
    return _find("admin")


def admin_status(*, include_private: bool = True) -> dict:
    row = admin_account()
    # Read the ledger's own checkpoint file: it is what `torchain` writes, so
    # the status panel answers instantly even when the node process is down
    # (calling the HTTP API here would block every login by the socket timeout).
    state = read_json(settings.chain_dir / "state.json", {}) or {}
    receiver = state.get("fee_receiver") if isinstance(state, dict) else None
    private = bool(row) and include_private
    return {
        "registered": bool(row),
        "registration_open": not row,
        "login_enabled": bool(row),
        "username": (row or {}).get("username") if private else None,
        "wallet": (row or {}).get("wallet") if private else None,
        "address": (row or {}).get("address") if private else None,
        "chain_token_bound": bool((row or {}).get("chain_token")) if private else None,
        "api_key_hint": (row or {}).get("api_key_hint") if private else None,
        "api_key_rotated_at": (row or {}).get("api_key_rotated_at") if private else None,
        "fee_receiver": receiver,
        "support_email": support_email(),
        "settings": ["password", "username", "api_key", "wallet", "delete"],
        "endpoints": {"register": "POST /api/admin/account/register",
                      "login": "POST /api/admin/account/login",
                      "status": "GET /api/admin/account"},
    }


def register_admin(username: object, password: object,
                   chain_token: object = None) -> dict:
    """First-run registration. Refused once an admin account exists."""
    if admin_account() is not None:
        raise Forbidden(
            "an admin account already exists — sign in, or delete it in "
            "Settings to re-open registration",
            details={"registration_open": False})
    name = clean_username(username)
    if find_username(name) is not None:
        raise ValidationError(f"username {name!r} is already taken")
    row = ACCOUNTS.add({
        "role": "admin", "username": name, "username_key": username_key(name),
        "password": hash_password(password),
        "wallet": None, "address": None,
        "chain_token": (_seal(str(password), str(chain_token).strip())
                        if str(chain_token or "").strip() else None),
        "api_key_sha3": None, "api_key_hint": None, "api_key_rotated_at": None,
        "created_at": _iso(), "updated_at": _iso()})
    opened = _open(row, str(password))
    return {"account": _public(row), **opened, "status": admin_status(),
            "support_email": support_email()}


def login_admin(username: object, password: object) -> dict:
    row = admin_account()
    if row is None:
        raise NotFound("no admin account is registered yet",
                       details={"register": "POST /api/admin/account/register"})
    if username_key(username) != username_key(row.get("username")) \
            or not check_password(password, row.get("password")):
        raise Forbidden("wrong username or password")
    return {"account": _public(row), **_open(row, str(password)),
            "status": admin_status(), "support_email": support_email()}


def _require_admin_row(password: object) -> dict:
    row = admin_account()
    if row is None:
        raise NotFound("no admin account is registered yet")
    if not check_password(password, row.get("password")):
        raise Forbidden("wrong password")
    return row


def change_password(current_password: object, new_password: object) -> dict:
    row = _require_admin_row(current_password)
    chain_token = _unseal(str(current_password), row.get("chain_token"))
    sealed = _seal(str(new_password), chain_token) if chain_token else row.get("chain_token")
    hashed = hash_password(new_password)   # validates the new password too
    updated = ACCOUNTS.update(str(row["id"]), password=hashed, chain_token=sealed,
                              updated_at=_iso())
    clear_sessions(str(row.get("username")))
    return {"account": _public(updated), "sessions_closed": True,
            "note": "sign in again with the new password"}


def change_username(password: object, new_username: object) -> dict:
    row = _require_admin_row(password)
    name = clean_username(new_username)
    other = find_username(name)
    if other is not None and other.get("id") != row.get("id"):
        raise ValidationError(f"username {name!r} is already taken")
    updated = ACCOUNTS.update(str(row["id"]), username=name,
                              username_key=username_key(name), updated_at=_iso())
    return {"account": _public(updated), "note": "the password is unchanged"}


def delete_admin(password: object) -> dict:
    """Delete the admin account; registration opens again."""
    row = _require_admin_row(password)
    ACCOUNTS.delete(str(row["id"]))
    closed = clear_sessions(str(row.get("username")))
    return {"deleted": True, "username": row.get("username"),
            "sessions_closed": closed, "registration_open": True,
            "wallet_kept": row.get("wallet"),
            "note": ("the account is gone; its wallet file stays so funds are "
                     "never destroyed — registration is enabled again")}


def rotate_api_key() -> dict:
    """Mint a new platform API key; the previous one stops working at once."""
    row = admin_account()
    if row is None:
        raise NotFound("no admin account is registered yet")
    key = "otk_" + secrets.token_urlsafe(32)
    hint = f"{key[:8]}…{key[-4:]}"
    digest = hashlib.sha3_256(key.encode("utf-8")).hexdigest()
    updated = ACCOUNTS.update(str(row["id"]), api_key_sha3=digest,
                              api_key_hint=hint, api_key_rotated_at=_iso(),
                              updated_at=_iso())
    return {"api_key": key, "hint": hint, "shown_once": True,
            "header": API_KEY_HEADER, "rotated_at": updated["api_key_rotated_at"],
            "note": ("this platform key now authorizes every admin route; the "
                     "chain's own admin token still works until you rotate it "
                     "on the node")}


def platform_key_matches(token: object) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    digest = hashlib.sha3_256(text.encode("utf-8")).hexdigest()
    with _LOCK:
        rows = ACCOUNTS.all()
    return any(hmac.compare_digest(digest, str(row.get("api_key_sha3") or ""))
               for row in rows if row.get("api_key_sha3"))


def generate_admin_wallet(chain_token: str | None) -> dict:
    """New admin wallet → fee collector; the account's previous wallet is deleted."""
    row = admin_account()
    if row is None:
        raise NotFound("no admin account is registered yet")
    if not str(chain_token or "").strip():
        raise Forbidden(
            "chain-level change: the admin account has no chain admin token "
            "bound. Register again with the node token, or send it in the "
            "x-admin-token header for this call.",
            details={"header": "x-admin-token",
                     "why": "the Rust node authorizes the fee-receiver change"})
    name = _wallet_name("admin_", str(row.get("username") or "admin"))
    created = ledger.new_wallet(name, 24)
    address = str(created.get("address") or "")
    changed = node.set_fee_receiver(address, token=str(chain_token))
    if not changed.get("ok"):
        _remove_wallet_file(name)
        error = changed.get("error") or {}
        raise ValidationError(
            f"the node refused the new fee receiver: {error.get('message', 'unknown')}",
            details={"node_error": error})
    previous = str(row.get("wallet") or "")
    removed = _remove_wallet_file(previous) if previous else False
    wallet = ledger.load_wallet(name)
    updated = ACCOUNTS.update(str(row["id"]), wallet=name, address=address,
                              updated_at=_iso())
    return {"account": _public(updated), "wallet": name, "address": address,
            "fee_receiver": changed.get("fee_receiver") or address,
            "previous_wallet": previous or None, "previous_wallet_deleted": removed,
            "one_time": {"private_key_base58": wallet.get("secret_key_base58"),
                         "mnemonic": wallet.get("mnemonic"), "shown_once": True},
            "note": ("the new wallet is the payment/fee collector; save the key "
                     "now, it is never shown again")}


def _remove_wallet_file(name: str) -> bool:
    """Delete one keystore file — never the founder/user wallets."""
    if not name or name in ("founder", "user1") or name.startswith("u_"):
        return False
    try:
        path = ledger.wallet_path(name)
    except Exception:  # noqa: BLE001 - a missing file is already "deleted"
        return False
    path.unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------------- users
def signup_user(username: object, password: object) -> dict:
    """Create a real wallet, return its private key once, open a session."""
    name = clean_username(username)
    if find_username(name) is not None:
        raise ValidationError(f"username {name!r} is already taken")
    hashed = hash_password(password)          # validate before touching the chain
    wallet_name = _wallet_name("u_", name)
    ledger.new_wallet(wallet_name, 24)        # real `torchain wallet new`
    wallet = ledger.load_wallet(wallet_name)
    address = str(wallet.get("address") or "")
    row = ACCOUNTS.add({
        "role": "user", "username": name, "username_key": username_key(name),
        "password": hashed, "wallet": wallet_name, "address": address,
        "wallets": [wallet_name],
        "wallet_meta": {wallet_name: {"origin": "generated", "label": "primary",
                                      "added_at": _iso()}},
        "chain_token": None, "api_key_sha3": None, "api_key_hint": None,
        "created_at": _iso(), "updated_at": _iso()})
    session = wallet_access.open_session(wallet_name, address)
    return {"account": _public(row), "wallet": {
                "name": wallet_name, "address": address,
                "pubkey_base58": wallet.get("pubkey_base58")},
            "one_time": {"private_key_base58": wallet.get("secret_key_base58"),
                         "mnemonic": wallet.get("mnemonic"), "shown_once": True,
                         "warning": ("copy this private key now — it is shown "
                                     "once and can never be retrieved again. "
                                     "Anyone who has it controls the wallet.")},
            "session": session, "support_email": support_email()}


def login_user(username: object, password: object) -> dict:
    row = find_username(username)
    if row is None or str(row.get("role")) != "user" \
            or not check_password(password, row.get("password")):
        raise Forbidden("wrong username or password")
    wallet_name = str(row.get("wallet") or "")
    wallet = ledger.load_wallet(wallet_name)
    session = wallet_access.open_session(wallet_name, str(wallet.get("address") or ""))
    return {"account": _public(row), "wallet": {
                "name": wallet_name, "address": wallet.get("address"),
                "pubkey_base58": wallet.get("pubkey_base58")},
            "session": session, "support_email": support_email()}


def user_for_wallet(wallet_name: str) -> dict | None:
    """The user account that owns ``wallet_name`` (primary or any sibling)."""
    wanted = str(wallet_name or "").strip()
    if not wanted:
        return None
    for row in ACCOUNTS.all():
        if str(row.get("role")) != "user":
            continue
        if str(row.get("wallet")) == wanted or wanted in account_wallets(row):
            return row
    return None


def user_profile(wallet_name: str, address: str = "") -> dict:
    row = user_for_wallet(wallet_name) or {}
    return {"account": _public(row) if row else None,
            "wallet": wallet_name, "address": address or row.get("address"),
            "support_email": support_email()}


# ------------------------------------------------------------ multiple wallets
def _clean_label(value: object, fallback: str) -> str:
    text = re.sub(r"[^\w .@:/+-]+", "", str(value or "")).strip()
    return (text or fallback)[:48]


def _require_user(wallet_name: object, password: object) -> dict:
    """The user account that owns ``wallet_name``, after the password check."""
    key = str(wallet_name or "")
    row = user_for_wallet(key)
    if row is None:
        raise Forbidden("sign in at /wallet first — only the account that owns "
                        "a wallet can add, export or import keys")
    now = time.time()
    with _LOCK:
        attempts = [stamp for stamp in _FAILS.get(key, [])
                    if now - stamp < FAIL_WINDOW]
        _FAILS[key] = attempts
    if len(attempts) >= FAIL_LIMIT:
        retry = max(1, int(FAIL_WINDOW - (now - attempts[0])))
        raise AppError(
            "too many wrong passwords — wait a few minutes and try again",
            code="TOO_MANY_ATTEMPTS", http_status=429,
            details={"retry_after_seconds": retry, "limit": FAIL_LIMIT})
    if not check_password(password, row.get("password")):
        with _LOCK:
            _FAILS.setdefault(key, []).append(now)
        raise Forbidden("wrong account password",
                        details={"wallet": key})
    with _LOCK:
        _FAILS.pop(key, None)
    return row


def _save_wallets(row: dict, names: list[str], meta: dict, *,
                  primary: str | None = None,
                  address: str | None = None) -> dict:
    fields = {"wallets": names[:MAX_WALLETS], "wallet_meta": meta,
              "updated_at": _iso()}
    if primary is not None:
        fields["wallet"] = primary
    if address is not None:
        fields["address"] = address
    return ACCOUNTS.update(str(row["id"]), **fields)


def _wallet_view(name: str, *, primary: bool, meta: dict) -> dict:
    try:
        view = ledger.public_wallet(name)
    except Exception as problem:  # noqa: BLE001 - one broken key must not hide the rest
        view = {"name": name, "address": None, "balance": 0, "balance_micro": 0,
                "balance_tor": "0", "tokens": {}, "warning": str(problem)[:200]}
    view["primary"] = bool(primary)
    view["meta"] = meta.get(name) or {}
    return view


def user_wallets(wallet_name: object) -> dict:
    """Every wallet the signed-in account owns, with public balances."""
    row = user_for_wallet(str(wallet_name or ""))
    if row is None:
        raise Forbidden("sign in at /wallet first",
                        details={"header": wallet_access.HEADER})
    meta = wallet_meta(row)
    names = account_wallets(row)
    primary = str(row.get("wallet") or "")
    rows = [_wallet_view(name, primary=name == primary, meta=meta) for name in names]
    return {"account": _public(row), "wallets": rows, "count": len(rows),
            "max_wallets": MAX_WALLETS, "primary": primary or None,
            "note": (f"an account holds up to {MAX_WALLETS} wallets — generated "
                     "on this node or imported with a private key / recovery "
                     "phrase. Every payment dropdown on the site lists them.")}


def generate_user_wallet(wallet_name: object, password: object,
                         *, label: object = "", words: object = 24) -> dict:
    """Add a freshly generated wallet (real ``torchain wallet new``) to the account."""
    row = _require_user(wallet_name, password)
    names = account_wallets(row)
    if len(names) >= MAX_WALLETS:
        raise ValidationError(
            f"this account already holds {MAX_WALLETS} wallets — the limit",
            details={"max_wallets": MAX_WALLETS, "wallets": names})
    slot = len(names) + 1
    name = _wallet_slot_name(str(row.get("username") or ""), slot)
    ledger.new_wallet(name, 12 if int(words or 24) == 12 else 24)
    wallet = ledger.load_wallet(name)
    meta = wallet_meta(row)
    meta[name] = {"origin": "generated",
                  "label": _clean_label(label, f"wallet {slot}"),
                  "added_at": _iso()}
    updated = _save_wallets(row, names + [name], meta)
    return {"account": _public(updated),
            "wallet": _wallet_view(name, primary=name == str(updated.get("wallet")),
                                   meta=meta),
            "one_time": {"private_key_base58": wallet.get("secret_key_base58"),
                         "mnemonic": wallet.get("mnemonic"), "shown_once": True,
                         "warning": ("copy this private key now — it can also be "
                                     "re-exported later with your account "
                                     "password")},
            "wallets": account_wallets(updated), "max_wallets": MAX_WALLETS}


def import_user_wallet(wallet_name: object, password: object, *,
                       secret_key_base58: object = None,
                       mnemonic: object = None, label: object = "") -> dict:
    """Attach an existing Phantom-compatible wallet to the account."""
    row = _require_user(wallet_name, password)
    names = account_wallets(row)
    if len(names) >= MAX_WALLETS:
        raise ValidationError(
            f"this account already holds {MAX_WALLETS} wallets — the limit",
            details={"max_wallets": MAX_WALLETS, "wallets": names})
    slot = len(names) + 1
    name = _wallet_slot_name(str(row.get("username") or ""), slot)
    imported = ledger.import_wallet(name, secret_key_base58=secret_key_base58,
                                    mnemonic=mnemonic)
    address = str(imported.get("address") or "")
    existing = {}
    for item in names:
        try:
            existing[str(ledger.load_wallet(item).get("address"))] = item
        except Exception:  # noqa: BLE001 - a broken sibling is simply skipped
            continue
    if address in existing:
        # never keep two keystores for one key: the first one stays the wallet
        try:
            ledger.wallet_path(name).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - nothing to clean up
            pass
        raise ValidationError(
            f"that wallet is already in this account as {existing[address]!r}",
            details={"wallet": existing[address], "address": address})
    meta = wallet_meta(row)
    meta[name] = {"origin": "imported",
                  "source": imported.get("imported_from"),
                  "label": _clean_label(label, f"imported wallet {slot}"),
                  "added_at": _iso()}
    updated = _save_wallets(row, names + [name], meta)
    return {"account": _public(updated), "imported": True,
            "wallet": _wallet_view(name, primary=name == str(updated.get("wallet")),
                                   meta=meta),
            "wallets": account_wallets(updated), "max_wallets": MAX_WALLETS,
            "note": ("verify the address matches the wallet you exported from "
                     "before sending funds to it")}


def export_user_wallet(wallet_name: object, password: object,
                       target: object = None) -> dict:
    """Password-authorized private-key export for one of the account's wallets."""
    row = _require_user(wallet_name, password)
    wanted = str(target or wallet_name or "").strip()
    if wanted not in account_wallets(row):
        raise Forbidden(f"wallet {wanted!r} does not belong to this account",
                        details={"wallets": account_wallets(row)})
    exported = ledger.export_wallet(wanted)
    return {**exported, "account": str(row.get("username") or ""),
            "note": ("this export is re-readable with the account password; "
                     "keep the key offline and never paste it into a chat")}


def select_user_wallet(wallet_name: object, target: object) -> dict:
    """Rotate the session onto another wallet the same account owns."""
    row = user_for_wallet(str(wallet_name or ""))
    if row is None:
        raise Forbidden("sign in at /wallet first",
                        details={"header": wallet_access.HEADER})
    wanted = str(target or "").strip()
    if wanted not in account_wallets(row):
        raise Forbidden(f"wallet {wanted!r} does not belong to this account",
                        details={"wallets": account_wallets(row)})
    wallet = ledger.load_wallet(wanted)
    address = str(wallet.get("address") or "")
    session = wallet_access.open_session(wanted, address)
    return {"account": _public(row),
            "wallet": _wallet_view(wanted, primary=wanted == str(row.get("wallet")),
                                   meta=wallet_meta(row)),
            "session": session, "wallets": account_wallets(row),
            "note": "the new session reads and spends from this wallet"}


def set_primary_user_wallet(wallet_name: object, target: object) -> dict:
    """Make another owned wallet the account's default (the signup wallet)."""
    row = user_for_wallet(str(wallet_name or ""))
    if row is None:
        raise Forbidden("sign in at /wallet first",
                        details={"header": wallet_access.HEADER})
    wanted = str(target or "").strip()
    if wanted not in account_wallets(row):
        raise Forbidden(f"wallet {wanted!r} does not belong to this account",
                        details={"wallets": account_wallets(row)})
    wallet = ledger.load_wallet(wanted)
    updated = _save_wallets(row, account_wallets(row), wallet_meta(row),
                            primary=wanted,
                            address=str(wallet.get("address") or ""))
    return {"account": _public(updated), "primary": wanted,
            "note": "new logins open this wallet; every wallet stays spendable"}



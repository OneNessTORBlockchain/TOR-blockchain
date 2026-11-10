"""Developer API keys — bought in TOR, required by the versioned API.

A key is a **paid, revocable credential for developers**, not an admin token:

* bought at ``POST /api/dev/keys`` from the signed-in account's wallet
  (``api_key`` in :mod:`core.pricing`, 25 TOR / 30 days, charged on-chain),
* shown **once**; only ``sha3-256(key)`` and a hint are stored,
* sent as ``x-api-key`` to ``/api/v1/<endpoint>``,
* rate-limited per minute/day (config ``dev_keys.*``), counted per key,
* revocable at once (``DELETE /api/dev/keys/<id>``).

A key can only call the *public* API surface. Anything that moves value needs
the wallet owner's session or signature, so a stolen key can read, never spend.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timezone

from core import pricing
from core.config import settings
from core.errors import AppError, Forbidden, NotFound, ValidationError
from oneness_tor.services.store import Collection, data_dir

KEYS = Collection(lambda: data_dir("dev_keys") / "keys.json", "devkey")

HEADER = "x-api-key"
KEY_PREFIX = "otd_"
STANDARD = "oneness-dev-key/1"
SCOPES = ("read",)

_LOCK = threading.RLock()
_USAGE: dict[str, dict] = {}


# --------------------------------------------------------------------- helpers
def _iso(stamp: float | int | None = None) -> str:
    moment = float(stamp if stamp is not None else time.time())
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


def digest(key: object) -> str:
    return hashlib.sha3_256(str(key or "").strip().encode("utf-8")).hexdigest()


def _days(value: object) -> int:
    default = int(settings.get("dev_keys.default_days") or 30)
    maximum = int(settings.get("dev_keys.max_days") or 365)
    try:
        days = int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        raise ValidationError("days must be a whole number, e.g. 30")
    if days < 1 or days > maximum:
        raise ValidationError(f"days must be between 1 and {maximum}",
                              details={"max_days": maximum})
    return days


def _limit(path: str, default: int) -> int:
    try:
        return max(1, int(settings.get(path) or default))
    except (TypeError, ValueError):
        return default


def limits() -> dict:
    return {"per_minute": _limit("dev_keys.rate_per_minute", 300),
            "per_day": _limit("dev_keys.rate_per_day", 50000)}


def public(row: dict) -> dict:
    """A key row without anything that could be replayed."""
    return {"id": row.get("id"), "label": row.get("label"),
            "hint": row.get("hint"), "wallet": row.get("wallet"),
            "address": row.get("address"), "scopes": row.get("scopes") or [],
            "active": bool(row.get("active", True)),
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "days": row.get("days"), "calls": int(row.get("calls") or 0),
            "last_used_at": row.get("last_used_at"),
            "last_path": row.get("last_path"),
            "charge": row.get("charge") or {},
            "note": ("send this key as the x-api-key header to /api/v1/<endpoint>; "
                     "only the hint is stored, so the key cannot be shown again")}


def pricing_doc() -> dict:
    spec = pricing.spec("api_key")
    limits_doc = limits()
    return {"standard": STANDARD, "header": HEADER, "prefix": KEY_PREFIX,
            "price": {"code": "api_key", "label": spec["label"],
                      "tor_per_30_days": spec["tor"],
                      "micro_tor_per_30_days": pricing.micro("api_key")},
            "limits": limits_doc, "scopes": list(SCOPES),
            "buy": "POST /api/dev/keys {label, days} with x-wallet-session",
            "use": "GET /api/v1/<endpoint> with x-api-key",
            "revoke": "DELETE /api/dev/keys/<id>",
            "note": ("keys read the public API only — spending still needs the "
                     "wallet owner's session or signature")}


# ------------------------------------------------------------------------ issue
def issue(wallet_name: str, *, label: object = "", days: object = None) -> dict:
    """Charge ``api_key`` on-chain and mint one key (returned exactly once)."""
    from oneness_tor.services import ledger

    period = _days(days)
    text = str(label or "").strip()[:48] or f"{period}-day key"
    quantity = max(1.0 / 30.0, period / 30.0)
    charge = ledger.charge(str(wallet_name), "api_key", quantity=quantity,
                           note=f"developer key · {text}")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + period * 86_400
    row = KEYS.add({
        "label": text, "wallet": str(wallet_name),
        "address": str(charge.get("from") or ""),
        "key_sha3": digest(key), "hint": f"{key[:10]}…{key[-4:]}",
        "scopes": list(SCOPES), "active": True, "days": period,
        "created_at": _iso(now), "created_unix": now,
        "expires_at": _iso(expires), "expires_unix": expires,
        "calls": 0, "last_used_at": None, "last_path": None,
        "charge": {"code": charge.get("code"), "txid": charge.get("txid"),
                   "amount_tor": charge.get("amount_tor"),
                   "amount_micro": charge.get("amount_micro"),
                   "fee_receiver": charge.get("fee_receiver")},
    })
    return {"key": key, "shown_once": True, "header": HEADER,
            "key_id": row["id"], "label": row["label"], "days": period,
            "expires_at": row["expires_at"], "scopes": row["scopes"],
            "charge": row["charge"], "limits": limits(),
            "use": (f"curl -H \"{HEADER}: {key}\" "
                    f"{str(settings.get('paths.public_base_url')).rstrip('/')}"
                    "/api/v1/chain.info"),
            "warning": ("copy the key now — only its sha3 hash is stored, so it "
                        "can never be shown again. Revoke it any time.")}


# ------------------------------------------------------------------------ reads
def rows_for(wallet_name: str) -> list[dict]:
    """Every key bought by any wallet of this account."""
    from oneness_tor.services import accounts

    row = accounts.user_for_wallet(str(wallet_name or ""))
    names = set(accounts.account_wallets(row)) if row else {str(wallet_name or "")}
    return [item for item in KEYS.all() if str(item.get("wallet")) in names]


def list_for(wallet_name: str) -> dict:
    rows = sorted(rows_for(wallet_name),
                  key=lambda item: int(item.get("created_unix") or 0), reverse=True)
    now = int(time.time())
    return {"keys": [public(item) for item in rows], "count": len(rows),
            "limits": limits(), "pricing": pricing_doc()["price"],
            "note": ("expired or revoked keys stay listed with active=false; "
                     "buy another key to keep calling /api/v1")}


def revoke(wallet_name: str, key_id: str) -> dict:
    rows = rows_for(wallet_name)
    match = next((item for item in rows if str(item.get("id")) == str(key_id)), None)
    if match is None:
        raise NotFound(f"key {key_id!r} is not one of this account's keys",
                       details={"account_wallet": str(wallet_name)})
    updated = KEYS.update(str(match["id"]), active=False, revoked_at=_iso(),
                          revoked_by=str(wallet_name))
    return {"revoked": True, "key": public(updated),
            "note": "the key stops working immediately; already-settled TOR is not refunded"}




# --------------------------------------------------------------------- verify
def find(presented: object) -> dict | None:
    """The row whose stored hash matches ``presented`` (constant-time)."""
    text = str(presented or "").strip()
    if not text.startswith(KEY_PREFIX):
        return None
    hashed = digest(text)
    for row in KEYS.all():
        stored = str(row.get("key_sha3") or "")
        if stored and hmac.compare_digest(stored, hashed):
            return row
    return None


def _window(row_id: str) -> dict:
    with _LOCK:
        return _USAGE.setdefault(row_id, {"minute_start": 0.0, "minute": 0,
                                          "day_start": 0.0, "day": 0})


def check_rate(row: dict) -> dict:
    """Raise 429 when this key is over its per-minute / per-day budget."""
    caps = limits()
    now = time.time()
    bucket = _window(str(row.get("id")))
    with _LOCK:
        if now - float(bucket["minute_start"]) >= 60:
            bucket["minute_start"], bucket["minute"] = now, 0
        if now - float(bucket["day_start"]) >= 86_400:
            bucket["day_start"], bucket["day"] = now, 0
        if bucket["minute"] >= caps["per_minute"]:
            retry = max(1, int(60 - (now - float(bucket["minute_start"]))))
            raise AppError(
                f"rate limit: {caps['per_minute']} calls/minute for this key",
                code="RATE_LIMITED", http_status=429,
                details={"retry_after_seconds": retry, "limits": caps})
        if bucket["day"] >= caps["per_day"]:
            retry = max(1, int(86_400 - (now - float(bucket["day_start"]))))
            raise AppError(
                f"rate limit: {caps['per_day']} calls/day for this key",
                code="RATE_LIMITED", http_status=429,
                details={"retry_after_seconds": retry, "limits": caps})
        bucket["minute"] += 1
        bucket["day"] += 1
    return {"minute": bucket["minute"], "day": bucket["day"], **caps}


def require(request) -> dict:
    """The live key for this request, or a 403 that says exactly how to get one."""
    presented = (request.headers.get(HEADER)
                 or request.args.get("api_key") or "").strip()
    if not presented:
        raise Forbidden(
            "a developer API key is required: buy one at /docs (25 TOR / 30 days) "
            "or POST /api/dev/keys, then send it as x-api-key",
            details={"header": HEADER, "buy": "POST /api/dev/keys",
                     "pricing": "GET /api/dev/keys/pricing"})
    row = find(presented)
    if row is None:
        raise Forbidden("that API key is not valid",
                        details={"header": HEADER,
                                 "hint": "keys start with " + KEY_PREFIX})
    if not bool(row.get("active", True)):
        raise Forbidden("this API key was revoked", details={"key_id": row.get("id")})
    expires = int(row.get("expires_unix") or 0)
    if expires and expires <= int(time.time()):
        raise Forbidden("this API key expired — buy a new one at /docs",
                        details={"key_id": row.get("id"),
                                 "expired_at": row.get("expires_at")})
    check_rate(row)
    return row


def budget(row: dict) -> dict:
    """Current window usage without consuming anything (for the UI)."""
    bucket = _window(str(row.get("id")))
    now = time.time()
    caps = limits()
    minute = bucket["minute"] if now - float(bucket["minute_start"]) < 60 else 0
    day = bucket["day"] if now - float(bucket["day_start"]) < 86_400 else 0
    return {"used_last_minute": minute, "used_today": day,
            "per_minute": caps["per_minute"], "per_day": caps["per_day"]}


def spend(row: dict, path: str, status: int) -> dict:
    """Record one call on the key (the file is the audit trail)."""
    now = int(time.time())
    updated = KEYS.update(str(row["id"]), calls=int(row.get("calls") or 0) + 1,
                          last_used_at=_iso(now), last_path=str(path)[:160],
                          last_status=int(status or 0), last_used_unix=now)
    row.update(updated)
    return updated

"""LOCKED price table (Section 8.3) — the single source of truth.

Nothing else in the codebase may hardcode a fee. Pages render these numbers,
the fee router enforces them, and ``GET /api/pricing`` serves them to the AI.

Section 8 prices are LOCKED. The meme-launcher rows are the launcher's own fee
schedule expressed in TOR (the original Solana module priced them in SOL); they
are overridable in ``config.yaml`` under ``pricing.overrides`` because the owner
may reprice them — the LOCKED rows are not overridable *from config*.

The owner reprices from the admin panel (``/admin`` → "Service fees"): the values
are persisted to ``data/admin/pricing.json`` and read here on the next lookup, so
a price change needs neither a restart nor an edit to a tracked file. A LOCKED
row only moves when the request carries ``confirm_locked: true`` — the published
default stays the reference, and every change is appended to the file's history
with the credential that made it.

Precedence for one row: the admin panel file, then ``config.yaml``
(``pricing.overrides``, unlocked rows only), then the published default.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import settings
from core.errors import ValidationError
from core.validator import MICRO_PER_TOR

#: A typo guard, not an economic limit: 1,000,000 TOR for one service.
MAX_TOR = 1_000_000.0
#: What a reset restores and what the history renders.
STORE_STANDARD = "oneness-tor-price-overrides/1"
STORE_NAME = "pricing.json"

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"mtime": None, "data": {"overrides": {}, "history": []}}

# code -> (label, TOR, locked, collected_by)
PRICE_TABLE: dict[str, dict] = {
    "contract_create": {
        "label": "Create a smart contract", "tor": 5.0, "locked": True,
        "collected_by": "fee receiver (founder wallet default)"},
    "mint": {
        "label": "Mint tokens via the integrated minter", "tor": 3.0,
        "locked": True, "collected_by": "fee receiver"},
    "transfer": {
        "label": "Node/platform transfer (the chain itself is fee-free)",
        "tor": 0.1, "locked": True,
        "collected_by": "fee receiver"},
    # ---- meme coin launcher (config-overridable) ----------------------
    "meme_token_create": {
        "label": "Meme coin create + metadata", "tor": 5.0, "locked": False,
        "collected_by": "fee receiver"},
    "meme_pool_create": {
        "label": "Launch liquidity pool (platform share)", "tor": 10.0,
        "locked": False, "collected_by": "fee receiver"},
    "meme_volume_session": {
        "label": "Volume & market bot — 15-minute session", "tor": 10.0,
        "locked": False, "collected_by": "fee receiver"},
    "meme_landing_page": {
        "label": "AI token landing page (generated + hosted)", "tor": 10.0,
        "locked": False, "collected_by": "fee receiver"},
    "meme_trending_day": {
        "label": "Trending promotion — per day", "tor": 5.0, "locked": False,
        "collected_by": "fee receiver"},
    "meme_banner_7d": {
        "label": "Banner advertisement — 7 days", "tor": 30.0, "locked": False,
        "collected_by": "fee receiver"},
    "nft_mint": {
        "label": "AI-NFT mint (1-of-1)", "tor": 3.0, "locked": False,
        "collected_by": "fee receiver"},
    "api_key": {
        "label": "Developer API key — 30 days", "tor": 25.0, "locked": False,
        "collected_by": "fee receiver"},
}


# --------------------------------------------------------------- override store
def store_path() -> Path:
    """``data/admin/pricing.json`` — the prices the admin panel set."""
    return settings.data_dir / "admin" / STORE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_store() -> dict:
    return {"standard": STORE_STANDARD, "updated_at": None,
            "overrides": {}, "history": []}


def _entry(code: str, value: object) -> dict | None:
    """One stored override as ``{tor, forced, set_at, set_by}`` (``None`` = invalid)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = {"tor": value}              # a hand-edited file: a bare number
    if not isinstance(value, dict):
        return None
    try:
        tor = float(value.get("tor"))
    except (TypeError, ValueError):
        return None
    if not 0 < tor <= MAX_TOR:
        return None
    locked = bool((PRICE_TABLE.get(code) or {}).get("locked"))
    return {"tor": tor, "forced": bool(value.get("forced")) or not locked,
            "set_at": value.get("set_at"), "set_by": value.get("set_by"),
            "default_tor": float((PRICE_TABLE.get(code) or {}).get("tor") or 0.0)}


def _normalise(raw: object) -> dict:
    """Whatever is on disk → the shape this module relies on (never raises)."""
    store = _empty_store()
    if not isinstance(raw, dict):
        return store
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        for code, value in overrides.items():
            entry = _entry(str(code), value)
            if entry is not None:
                store["overrides"][str(code)] = entry
    history = raw.get("history")
    if isinstance(history, list):
        store["history"] = [row for row in history if isinstance(row, dict)][-200:]
    store["updated_at"] = raw.get("updated_at")
    return store


def _store() -> dict:
    """The on-disk overrides, re-read only when the file changed."""
    path = store_path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _empty_store()
    with _LOCK:
        if _CACHE.get("mtime") == stamp:
            return _CACHE["data"]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None                    # a broken file must never break pricing
        data = _normalise(raw)
        _CACHE.update(mtime=stamp, data=data)
        return data


def _save(store: dict) -> dict:
    """Atomic write (temp file + ``os.replace``) then refresh the cache."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"standard": STORE_STANDARD, "updated_at": _now(),
                "overrides": store.get("overrides") or {},
                "history": list(store.get("history") or [])[-200:]}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    os.replace(temporary, path)
    with _LOCK:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = None
        _CACHE.update(mtime=stamp, data=_normalise(document))
    return document


def overrides() -> dict[str, dict]:
    """The live admin-panel overrides, keyed by price code."""
    return {code: dict(entry) for code, entry in _store()["overrides"].items()}


def _configured(code: str) -> float | None:
    block = settings.get("pricing.overrides") or {}
    if not isinstance(block, dict) or code not in block:
        return None
    try:
        return float(block[code])
    except (TypeError, ValueError):
        return None


def _current(code: str, spec: dict) -> tuple[float, str]:
    """``(price in TOR, who set it)`` — admin panel, then config, then default."""
    entry = _store()["overrides"].get(code)
    if entry and (entry.get("forced") or not spec["locked"]):
        return float(entry["tor"]), "admin"
    if not spec["locked"]:
        configured = _configured(code)
        if configured is not None:
            return configured, "config"
    return float(spec["tor"]), "default"


def _override(code: str, spec: dict) -> dict:
    tor, source = _current(code, spec)
    default = float(spec["tor"])
    return {**spec, "tor": tor, "default_tor": default, "source": source,
            "overridden": abs(tor - default) > 1e-12}


def spec(code: str) -> dict:
    """Full price spec for ``code`` or raise ValidationError."""
    if code not in PRICE_TABLE:
        raise ValidationError(f"unknown price code {code!r}",
                             details={"known": sorted(PRICE_TABLE)})
    return _override(code, PRICE_TABLE[code])


def price(code: str) -> float:
    """Price in TOR for one unit of ``code``."""
    return float(spec(code)["tor"])


def micro(code: str, quantity: int | float = 1) -> int:
    """Price in microTOR for ``quantity`` units (rounded once, never twice)."""
    return int(round(price(code) * float(quantity) * MICRO_PER_TOR))


def table() -> list[dict]:
    """Every row at its live price, sorted LOCKED-first then alphabetical."""
    rows = []
    for code in PRICE_TABLE:
        item = _override(code, PRICE_TABLE[code])
        rows.append({"code": code, "label": item["label"],
                     "tor": item["tor"],
                     "micro_tor": int(round(item["tor"] * MICRO_PER_TOR)),
                     "default_tor": item["default_tor"],
                     "locked": item["locked"], "overridden": item["overridden"],
                     "source": item["source"],
                     "collected_by": item["collected_by"]})
    rows.sort(key=lambda row: (not row["locked"], row["code"]))
    return rows


def defaults_table() -> list[dict]:
    """The table exactly as published — what a reset restores, plus who set it."""
    rows = [{"code": code, "label": spec["label"], "tor": float(spec["tor"]),
             "micro_tor": int(round(float(spec["tor"]) * MICRO_PER_TOR)),
             "locked": spec["locked"], "collected_by": spec["collected_by"]}
            for code, spec in PRICE_TABLE.items()]
    rows.sort(key=lambda row: (not row["locked"], row["code"]))
    return rows


def apply(rows: object) -> list[dict]:
    """Overlay the live prices onto another table (the Rust node's own copy).

    ``torchain`` ships the same LOCKED table in Rust and ``GET /api/pricing``
    prefers the node's answer. The admin panel reprices here, so every row this
    module knows is replaced by the live value — otherwise the published price
    and the fee actually charged (always Python-side) could disagree. Rows this
    module does not know are passed through untouched.
    """
    merged: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "")
        if code not in PRICE_TABLE:
            merged.append(row)
            continue
        live = _override(code, PRICE_TABLE[code])
        merged.append({**row, "tor": live["tor"],
                       "micro_tor": int(round(live["tor"] * MICRO_PER_TOR)),
                       "label": row.get("label") or live["label"],
                       "locked": live["locked"],
                       "default_tor": live["default_tor"],
                       "overridden": live["overridden"],
                       "source": live["source"],
                       "collected_by": row.get("collected_by")
                       or live["collected_by"]})
    return merged


def as_dict() -> dict:
    """``{code: tor}`` convenience map."""
    return {row["code"]: row["tor"] for row in table()}


def _validate(code: str, tor: object) -> float:
    if code not in PRICE_TABLE:
        raise ValidationError(f"unknown price code {code!r}",
                              details={"known": sorted(PRICE_TABLE)})
    try:
        value = float(tor)                      # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError(f"{code}: the price must be a number in TOR",
                              details={"received": tor}) from None
    if not value > 0:
        raise ValidationError(f"{code}: the price must be greater than zero",
                              details={"received": value})
    if value > MAX_TOR:
        raise ValidationError(f"{code}: the price must be at most {MAX_TOR:g} TOR",
                              details={"received": value})
    return round(value, 8)


def set_price(code: str, tor: object, *, confirm_locked: bool = False,
              actor: str = "admin") -> dict:
    """Reprice one service and persist it; returns that row at its new price.

    A LOCKED Section 8 row refuses without ``confirm_locked`` — those numbers are
    published, so moving one is a deliberate act, written to the store's history
    together with the credential that asked for it.
    """
    value = _validate(code, tor)
    spec = PRICE_TABLE[code]
    if spec["locked"] and not confirm_locked:
        raise ValidationError(
            f"{code} is a LOCKED Section 8 price ({float(spec['tor']):g} TOR) — "
            f"send confirm_locked: true to move it deliberately",
            code="PRICE_LOCKED",
            details={"code": code, "default_tor": float(spec["tor"]),
                     "locked": True})
    with _LOCK:
        store = _store()
        previous, previous_source = _current(code, spec)
        entries = dict(store["overrides"])
        entries[code] = {"tor": value,
                         "forced": bool(spec["locked"] and confirm_locked),
                         "set_at": _now(), "set_by": str(actor or "admin")[:80],
                         "default_tor": float(spec["tor"])}
        history = list(store["history"]) + [{
            "code": code, "label": spec["label"], "from": previous, "to": value,
            "from_source": previous_source, "at": _now(),
            "by": str(actor or "admin")[:80], "locked": bool(spec["locked"])}]
        _save({**store, "overrides": entries, "history": history})
    row = next(item for item in table() if item["code"] == code)
    return {**row, "previous_tor": previous, "previous_source": previous_source}


def reset_price(code: str, *, actor: str = "admin") -> dict:
    """Drop the admin override for one row (back to config or the default)."""
    if code not in PRICE_TABLE:
        raise ValidationError(f"unknown price code {code!r}",
                              details={"known": sorted(PRICE_TABLE)})
    spec = PRICE_TABLE[code]
    with _LOCK:
        store = _store()
        entries = dict(store["overrides"])
        removed = entries.pop(code, None)
        history = list(store["history"])
        if removed is not None:
            history.append({"code": code, "label": spec["label"],
                            "from": float(removed["tor"]),
                            "to": float(spec["tor"]), "from_source": "admin",
                            "at": _now(), "by": str(actor or "admin")[:80],
                            "locked": bool(spec["locked"]), "reset": True})
            _save({**store, "overrides": entries, "history": history})
    row = next(item for item in table() if item["code"] == code)
    return {**row, "removed": removed is not None}


def reset_all(*, actor: str = "admin") -> dict:
    """Clear every admin override — the published table comes back."""
    with _LOCK:
        store = _store()
        entries = dict(store["overrides"])
        history = list(store["history"])
        for code, entry in entries.items():
            history.append({"code": code,
                            "label": (PRICE_TABLE.get(code) or {}).get("label"),
                            "from": float(entry["tor"]), "to": None,
                            "from_source": "admin", "at": _now(),
                            "by": str(actor or "admin")[:80], "reset": True})
        _save({**store, "overrides": {}, "history": history})
    return admin_table()


def admin_table() -> dict:
    """Everything the admin panel renders: live prices, defaults and history."""
    store = _store()
    rows = []
    for code in PRICE_TABLE:
        spec = PRICE_TABLE[code]
        live = _override(code, spec)
        entry = store["overrides"].get(code) or {}
        rows.append({"code": code, "label": spec["label"], "tor": live["tor"],
                     "micro_tor": int(round(live["tor"] * MICRO_PER_TOR)),
                     "default_tor": live["default_tor"],
                     "locked": bool(spec["locked"]),
                     "overridden": live["overridden"], "source": live["source"],
                     "collected_by": spec["collected_by"],
                     "set_at": entry.get("set_at"), "set_by": entry.get("set_by"),
                     "forced": bool(entry.get("forced")) and bool(spec["locked"])})
    rows.sort(key=lambda row: (not row["locked"], row["code"]))
    return {"rows": rows, "count": len(rows),
            "overrides": len(store["overrides"]),
            "store": str(store_path()), "updated_at": store["updated_at"],
            "history": store["history"][-25:],
            "locked_codes": [code for code, spec in PRICE_TABLE.items()
                             if spec["locked"]],
            "note": ("these are the prices the fee router charges and every page "
                     "renders. LOCKED Section 8 rows need confirm_locked: true; a "
                     "reset restores the published value. The Rust node keeps its "
                     "own copy of the table — /api/pricing overlays these values "
                     "on top of it, so the numbers can never disagree")}

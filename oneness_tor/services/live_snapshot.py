"""The live numbers the HTML prints before — or without — any JavaScript.

Tor Browser blocks scripts on plain ``http://`` onion pages at the *Safer* and
*Safest* security levels. A dashboard whose numbers only arrive through
``/api/*`` therefore looks dead while the node behind it is perfectly healthy.
This module reads exactly the sources the endpoints read and hands the templates
a snapshot they print straight into their own HTML, so height, chain, supply,
models, revenue and Tor control state are on screen with scripts off. When
scripts *are* on, ``app.js`` and the page scripts overwrite the same elements
with fresher values — the markup is the same either way.

    from oneness_tor.services import live_snapshot
    live_snapshot.SNAPSHOT_TTL
    live_snapshot.snapshot()      # cached for SNAPSHOT_TTL seconds
    live_snapshot.clear()         # tests: drop the cache

Every probe is wrapped: a node that is down, an Ollama that is not serving or a
Tor control port that is closed degrade one field each and are reported in
``errors`` — the page still renders.
"""
from __future__ import annotations

import threading
import time

from core import api_catalog, ollama
from core.errors import AppError, ChainUnavailable
from core.node_client import node
from core.validator import format_tor
from oneness_tor.services import payments, tor_service, unwrap

__all__ = ["SNAPSHOT_TTL", "PAGE_TIMEOUT", "snapshot", "clear"]

#: How long one snapshot is reused. Long enough that a page render never waits
#: on a probe twice in a row, short enough that a reload shows the real height.
SNAPSHOT_TTL = 5.0

#: The page may not hang on a slow node: three seconds, then "unreachable".
PAGE_TIMEOUT = 3.0

_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "value": None}


def clear() -> None:
    """Forget the cached snapshot (tests, or after a settings change)."""
    with _LOCK:
        _CACHE["at"], _CACHE["value"] = 0.0, None


def snapshot(*, ttl: float | None = None) -> dict:
    """The current snapshot, refreshed at most once every ``ttl`` seconds."""
    now = time.monotonic()
    window = SNAPSHOT_TTL if ttl is None else float(ttl)
    with _LOCK:
        cached = _CACHE["value"]
        if cached is not None and now - float(_CACHE["at"]) < window:
            return cached
    value = _decorate(_probe())
    with _LOCK:
        _CACHE["value"], _CACHE["at"] = value, time.monotonic()
    return value


# ------------------------------------------------------------------ formatting
def _int_text(value: object) -> str:
    """``33`` → ``"33"``, ``3300`` → ``"3,300"``, ``None`` → ``"—"``."""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}"          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)


def _tor_text(micro: object) -> str:
    """MicroTOR → the trimmed TOR display string the API already returns."""
    if micro is None or micro == "":
        return "—"
    try:
        return format_tor(int(micro))      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(micro)


def _short(value: object, size: int = 14) -> str:
    """The same abbreviation ``OT.short`` uses: first N, ellipsis, last six."""
    text = "" if value is None else str(value)
    return f"{text[:size]}…{text[-6:]}" if len(text) > size * 2 else (text or "—")


def _ago(at: object) -> str:
    if not at:
        return "unknown"
    age = max(0, int(time.time() - int(at)))    # type: ignore[arg-type]
    if age < 5:
        return "just now"
    if age < 90:
        return f"{age}s ago"
    return f"{age // 60} min ago"


def _message(problem: object) -> str:
    return str(getattr(problem, "message", "") or problem)


# --------------------------------------------------------------------- probing
def _probe() -> dict:
    """Read every source once; never raise."""
    raw: dict = {
        "at": int(time.time()),
        "node_url": node.base_url,
        "node_reachable": False,
        "height": None, "chain_id": None, "head_hash": None,
        "genesis_hash": None, "founder": None, "fee_receiver": None,
        "supply_micro": None, "founder_micro": None,
        "endpoints": len(api_catalog.ALL),
        "ollama": {"reachable": False, "model_count": 0, "version": None,
                   "default_model": None},
        "tor": {"reachable": False, "installed": False, "services": [],
                "control_port": None},
        "revenue_micro": 0, "revenue_count": 0,
        "errors": {},
    }
    info = _chain_info(raw)
    if info:
        _founder_balance(raw, info.get("founder"))
    _ollama_state(raw)
    _tor_state(raw)
    _revenue(raw)
    return raw


def _chain_info(raw: dict) -> dict:
    try:
        info = unwrap(node.call("GET", "/api/chain/info", timeout=PAGE_TIMEOUT))
    except (ChainUnavailable, AppError) as problem:
        raw["errors"]["chain"] = _message(problem)
        return {}
    raw["node_reachable"] = True
    for key in ("height", "chain_id", "head_hash", "genesis_hash", "founder",
                "fee_receiver", "total_supply_micro"):
        if key in info:
            target = "supply_micro" if key == "total_supply_micro" else key
            raw[target] = info[key]
    return info


def _founder_balance(raw: dict, founder: object) -> None:
    if not founder:
        return
    try:
        account = unwrap(node.call("GET", f"/api/account/{founder}",
                                   timeout=PAGE_TIMEOUT))
    except (ChainUnavailable, AppError) as problem:
        raw["errors"]["account"] = _message(problem)
        return
    raw["founder_micro"] = account.get("balance_micro")


def _ollama_state(raw: dict) -> None:
    try:
        status = ollama.status()
    except Exception as problem:                 # noqa: BLE001 - never page-fatal
        raw["errors"]["ollama"] = _message(problem)
        return
    rows = status.get("models") or []
    raw["ollama"] = {
        "reachable": bool(status.get("reachable")),
        "installed": bool(status.get("installed")),
        "model_count": int(status.get("model_count") or len(rows)),
        "version": status.get("version"),
        "default_model": status.get("default_model"),
        "error": status.get("error"),
    }


def _tor_state(raw: dict) -> None:
    try:
        status = tor_service.status()
    except Exception as problem:                 # noqa: BLE001 - never page-fatal
        raw["errors"]["tor"] = _message(problem)
        return
    raw["tor"] = {
        "reachable": bool(status.get("reachable")),
        "installed": bool(status.get("installed")),
        "services": status.get("services") or [],
        "control_port": status.get("control_port"),
        "socks_port": status.get("socks_port"),
        "version": status.get("version"),
        "error": status.get("error"),
    }
    raw["socks_port"] = status.get("socks_port")


def _revenue(raw: dict) -> None:
    try:
        money = payments.revenue()
    except Exception as problem:                 # noqa: BLE001 - never page-fatal
        raw["errors"]["revenue"] = _message(problem)
        return
    raw["revenue_micro"] = int(money.get("total_micro") or 0)
    raw["revenue_count"] = int(money.get("count") or 0)


# ------------------------------------------------------------------ decorating
def _decorate(raw: dict) -> dict:
    """Add the exact strings the templates print (and ``app.js`` would write)."""
    snap = dict(raw)
    snap["when"] = _ago(raw.get("at"))
    snap["height_text"] = _int_text(raw.get("height"))
    snap["supply_text"] = _tor_text(raw.get("supply_micro"))
    snap["founder_text"] = _tor_text(raw.get("founder_micro"))
    snap["revenue_text"] = _tor_text(raw.get("revenue_micro"))
    snap["head_short"] = _short(raw.get("head_hash"))
    snap["genesis_short"] = _short(raw.get("genesis_hash"))
    snap["founder_short"] = _short(raw.get("founder"), 10)
    snap["fee_receiver"] = raw.get("fee_receiver") or "—"
    snap["chain_text"] = (f"{raw.get('chain_id')} · head {snap['head_short']}"
                          if raw.get("chain_id") else "reading the node")

    state = snap.setdefault("ollama", {})
    state["text"] = (f"{state.get('model_count', 0)} models · "
                     f"{'up' if state.get('reachable') else 'down'}")

    tor = snap.setdefault("tor", {})
    tor["text"] = "up" if tor.get("reachable") else "off"
    if tor.get("reachable"):
        tor["detail"] = f"{len(tor.get('services') or [])} hidden services"
    elif tor.get("installed"):
        tor["detail"] = f"control :{tor.get('control_port')} closed"
    else:
        tor["detail"] = "tor not installed"
    return snap

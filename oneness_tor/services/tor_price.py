"""TOR's USD price — one 30-second volatility feed for every platform.

The Capitain's order (2026-11-15) replaces the old "anchor x turnover drift"
quote with a live, volatile one, and keeps it honest:

* **one tick every 30 s** — ``market.price_tick_seconds``;
* **the price moves by a random step of $0.10 to $0.50** — that is the whole
  volatility budget, and it is never exceeded (``market.price_step_min_usd`` /
  ``market.price_step_max_usd``);
* **the direction follows real activity** — the 24 h swap count and TOR volume
  the ledger reports push the price up, a quiet chain pulls it down;
* **the quote is clamped to $3.00 - $100.00** — the floor and the cap are
  absolute (``market.price_floor_usd`` / ``market.price_cap_usd``), so no tick,
  no publish and no restart can leave the band;
* **the Marketcap & Price Engine is the authority** — when it publishes a quote
  (``POST /api/market/tor/price``) this node serves that number to every
  platform until the next tick, and only walks the price itself when the engine
  is silent: a chain must never be priceless.

The state lives in ``data/market/tor-price.json``, so a restart resumes the same
price instead of jumping back to the anchor. The last 2 880 samples (24 h at
30 s) are kept for the chart.
"""
from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime, timezone

from core.config import settings
from core.errors import ValidationError
from oneness_tor.services.store import data_dir, read_json, write_json

STANDARD = "oneness-tor-price-walk/1"
LOCK = threading.RLock()
_RNG = random.SystemRandom()

STEP_MIN_USD = 0.10
STEP_MAX_USD = 0.50
FLOOR_USD = 3.00
CAP_USD = 100.00
TICK_SECONDS = 30.0
HISTORY_POINTS = 2880
MAX_CATCH_UP_TICKS = 240

RULE = ("one tick every 30 s; step = random $0.10-$0.50 per tick, direction biased by "
        "24 h swaps + TOR volume; clamp $3.00-$100.00; the price engine's published "
        "quote wins while it is fresh")


def _number(value: object, fallback: float = 0.0) -> float:
    try:
        number = float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _setting(name: str, default: float) -> float:
    return _number(settings.get(name), default) or default


def tick_seconds() -> float:
    return max(5.0, _setting("market.price_tick_seconds", TICK_SECONDS))


def step_min_usd() -> float:
    return max(0.01, _setting("market.price_step_min_usd", STEP_MIN_USD))


def step_max_usd() -> float:
    return max(step_min_usd(), _setting("market.price_step_max_usd", STEP_MAX_USD))


def floor_usd() -> float:
    return max(0.01, _setting("market.price_floor_usd", FLOOR_USD))


def cap_usd() -> float:
    return max(floor_usd(), _setting("market.price_cap_usd", CAP_USD))


def activity_volume_tor() -> float:
    return max(0.01, _setting("market.activity_volume_tor", 1000.0))


def activity_swaps() -> float:
    return max(1.0, _setting("market.activity_swaps", 250.0))


def _history_points() -> int:
    return int(max(30, _setting("market.price_history_points", HISTORY_POINTS)))


def state_path():
    return data_dir("market") / "tor-price.json"


def _iso(stamp: float | None = None) -> str:
    return datetime.fromtimestamp(stamp if stamp is not None else time.time(),
                                  timezone.utc).isoformat(timespec="seconds")


def clamp(price: float) -> float:
    return round(max(floor_usd(), min(cap_usd(), _number(price))), 8)


def activity_score(*, volume_tor: float = 0.0, swaps: float = 0.0) -> dict:
    """0..1 — how busy the ledger is, from the 24 h record it already holds."""
    volume_part = min(1.0, max(0.0, _number(volume_tor)) / activity_volume_tor())
    swap_part = min(1.0, max(0.0, _number(swaps)) / activity_swaps())
    score = round(min(1.0, 0.6 * volume_part + 0.4 * swap_part), 6)
    return {"score": score, "volume_24h_tor": round(_number(volume_tor), 8),
            "swaps_24h": int(_number(swaps)),
            "volume_target_tor": activity_volume_tor(),
            "swaps_target": int(activity_swaps())}


def activity_target(score: float) -> float:
    """The price the current activity deserves, inside the $3 - $100 band."""
    span = cap_usd() - floor_usd()
    return round(floor_usd() + min(1.0, max(0.0, _number(score))) * span, 8)


def upward_bias(price: float, target: float) -> float:
    """Mean-reverting odds: above the target most ticks fall, below it most rise."""
    price = max(_number(price), 1e-9)
    target = max(_number(target), 1e-9)
    span = max(price, target)
    return round(max(0.05, min(0.95, 0.5 + 0.5 * (target - price) / span)), 6)


def next_price(price: float, target: float, *, rng: random.Random | None = None) -> dict:
    """One tick: a random $0.10-$0.50 step, biased by activity, clamped."""
    source = rng or _RNG
    low, high = step_min_usd(), step_max_usd()
    step = source.uniform(low, high)
    bias = upward_bias(price, target)
    up = source.random() < bias
    before = clamp(price)
    after = clamp(before + step if up else before - step)
    return {
        "price_usd": after,
        "step_usd": round(abs(after - before), 8),
        "direction": "up" if after >= before else "down",
        "bias": bias,
        "target_usd": round(target, 8),
        "clamped": after in (floor_usd(), cap_usd()),
    }


def _blank(anchor: float, now: float) -> dict:
    return {"standard": STANDARD, "price_usd": clamp(anchor), "anchor_usd": anchor,
            "source": "node-volatility", "updated_ts": now, "updated_at": _iso(now),
            "tick": 0, "step_usd": 0.0, "direction": "flat", "bias": 0.5,
            "target_usd": clamp(anchor), "clamped": False,
            "activity": activity_score(), "history": [[int(now), clamp(anchor)]],
            "published_by": None}


def _load(anchor: float, now: float | None = None) -> dict:
    document = read_json(state_path(), {}) or {}
    if not isinstance(document, dict) or not document.get("price_usd"):
        return _blank(anchor, now if now is not None else time.time())
    document["price_usd"] = clamp(document.get("price_usd"))
    history = document.get("history")
    if not isinstance(history, list):
        history = []
    document["history"] = [point for point in history
                           if isinstance(point, list) and len(point) == 2][-_history_points():]
    return document


def _save(document: dict) -> None:
    write_json(state_path(), document)


def anchor_default() -> float:
    return _number(settings.get("pricing.tor_usd"), 3.60) or 3.60


def _advance(document: dict, now: float, target: float) -> dict:
    """Walk the price forward to ``now``, one 30 s tick at a time (bounded)."""
    tick = tick_seconds()
    elapsed = max(0.0, now - _number(document.get("updated_ts"), now))
    due = min(MAX_CATCH_UP_TICKS, int((elapsed + 1.0) // tick))
    price = clamp(document.get("price_usd"))
    for index in range(due):
        move = next_price(price, target)
        price = move["price_usd"]
        document.update({key: move[key] for key in
                         ("step_usd", "direction", "bias", "target_usd", "clamped")})
        document["tick"] = int(_number(document.get("tick"))) + 1
        stamp = int(now - (due - index - 1) * tick)
        document.setdefault("history", []).append([stamp, price])
    document["price_usd"] = price
    history = document.get("history") or []
    if len(history) > _history_points():
        document["history"] = history[-_history_points():]
    document["source"] = "node-volatility"
    document["updated_ts"] = now
    document["updated_at"] = _iso(now)
    document["anchor_usd"] = _number(document.get("anchor_usd")) or anchor_default()
    return document


def view(document: dict, *, now: float | None = None) -> dict:
    """The API block every platform reads — never a bare number."""
    stamp = now if now is not None else time.time()
    tick = tick_seconds()
    updated = _number(document.get("updated_ts"), stamp)
    history = [point for point in (document.get("history") or [])
               if isinstance(point, list) and len(point) == 2]
    return {
        "standard": STANDARD,
        "symbol": "TOR", "name": "OneNess TOR", "quote": "USD",
        "price_usd": clamp(document.get("price_usd")),
        "price_tor": 1.0,
        "anchor_usd": round(_number(document.get("anchor_usd")) or anchor_default(), 8),
        "floor_usd": floor_usd(), "cap_usd": cap_usd(),
        "update_seconds": tick,
        "updated_at": document.get("updated_at") or _iso(updated),
        "updated_ts": int(updated),
        "next_update_at": _iso(updated + tick),
        "source": document.get("source") or "node-volatility",
        "published_by": document.get("published_by"),
        "tick": int(_number(document.get("tick"))),
        "step_usd": round(_number(document.get("step_usd")), 8),
        "direction": document.get("direction") or "flat",
        "bias": round(_number(document.get("bias"), 0.5), 6),
        "target_usd": round(_number(document.get("target_usd")), 8),
        "clamped": bool(document.get("clamped")),
        "activity": document.get("activity") or activity_score(),
        "history": [[int(point[0]), clamp(point[1])] for point in history[-180:]],
        "history_points": len(history),
        "rule": RULE,
        "engine_fresh": document.get("source") == "engine",
        "engine_stale_after_s": tick * 2,
    }


def quote(*, volume_tor: float = 0.0, swaps: float = 0.0,
          liquidity_tor: float = 0.0, anchor_usd: float | None = None,
          now: float | None = None) -> dict:
    """This tick's TOR/USD quote, advancing the walk when the tick is due."""
    stamp = now if now is not None else time.time()
    tick = tick_seconds()
    activity = activity_score(volume_tor=volume_tor, swaps=swaps)
    activity["liquidity_tor"] = round(_number(liquidity_tor), 8)
    target = activity_target(activity["score"])
    with LOCK:
        document = _load(anchor_usd or anchor_default(), stamp)
        fresh_engine = (document.get("source") == "engine"
                        and stamp - _number(document.get("updated_ts")) < tick * 2)
        if not fresh_engine and stamp - _number(document.get("updated_ts")) >= tick:
            document = _advance(document, stamp, target)
        elif not fresh_engine:
            document["target_usd"] = target
        document["activity"] = activity
        _save(document)
        return view(document, now=stamp)


def publish(price_usd: object, *, activity: dict | None = None, actor: str = "",
            source: str = "engine", now: float | None = None) -> dict:
    """Accept the Price Engine's quote and serve it to every platform."""
    price = _number(price_usd, -1.0)
    if price < 0:
        raise ValidationError("price_usd must be a number", details={"field": "price_usd"})
    if not floor_usd() <= price <= cap_usd():
        raise ValidationError(
            f"price_usd must sit inside the published band ${floor_usd():.2f}-${cap_usd():.2f}",
            details={"field": "price_usd", "received": price,
                     "floor_usd": floor_usd(), "cap_usd": cap_usd()})
    stamp = now if now is not None else time.time()
    with LOCK:
        document = _load(anchor_default(), stamp)
        before = clamp(document.get("price_usd"))
        after = clamp(price)
        document.update({
            "standard": STANDARD, "price_usd": after, "source": source,
            "published_by": str(actor or "")[:80] or None,
            "step_usd": round(abs(after - before), 8),
            "direction": "up" if after >= before else "down",
            "clamped": False, "updated_ts": stamp, "updated_at": _iso(stamp),
            "tick": int(_number(document.get("tick"))) + 1,
            "target_usd": after,
        })
        if isinstance(activity, dict):
            document["activity"] = {**activity_score(), **activity}
        history = document.setdefault("history", [])
        history.append([int(stamp), after])
        document["history"] = history[-_history_points():]
        _save(document)
        return view(document, now=stamp)


def peek(*, now: float | None = None) -> dict:
    """The current quote, read-only — never advances the walk."""
    with LOCK:
        return view(_load(anchor_default()), now=now)


def status() -> dict:
    """A tiny health block for /api/health style consumers."""
    with LOCK:
        block = view(_load(anchor_default()))
    return {"standard": STANDARD, "price_usd": block["price_usd"],
            "updated_at": block["updated_at"], "source": block["source"],
            "update_seconds": block["update_seconds"], "rule": RULE,
            "floor_usd": block["floor_usd"], "cap_usd": block["cap_usd"]}

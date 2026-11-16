"""Price & market-cap engine — one quote for every TOR platform.

Every number here is derived from chain state, never typed in:

* **TOR's USD price** is the 30-second volatility feed in
  ``oneness_tor.services.tor_price``: one random step of $0.10-$0.50 per tick,
  direction biased by the 24 h swaps and TOR volume the ledger reports, clamped
  to a $3.00 floor and a $100.00 cap. The quote (and the tick it belongs to) is
  reported next to the price, and the Marketcap & Price Engine can publish the
  authoritative number through ``POST /api/market/tor/price``. The old
  turnover drift is still published as ``drift`` for older clients, but it no
  longer sets the price.
* **A token's price** is its own AMM reserve ratio: ``price_tor`` from the
  Rust ledger's ``TOR ⟷ token`` pool, or the book's ``USDC ⟷ token`` ratio for
  the 50 USDC pairs. When both exist, the deeper pool wins and the other is
  listed beside it.
* **Market cap** is ``price × on-chain supply`` (never circulating guesswork);
  **liquidity** is the quote-side reserve doubled; **24 h volume** is the sum of
  real swap records the ledger holds.

Charts are sampled from those same live prices (``data/market/history.json``),
so a candle is a real observation, and a token with one observation charts as a
flat line — labelled ``samples`` with the observation count, never filled with
invented candles.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from core.config import settings
from oneness_tor.services import pool_books, token_registry, tor_price
from oneness_tor.services.store import data_dir, read_json, write_json

STANDARD = "oneness-tor-market/1"
LOCK = threading.RLock()
_CACHE: dict[str, object] = {"at": 0.0, "payload": None}
_LAST_SAMPLE = {"at": 0.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def anchor_usd() -> float:
    """The launch price of TOR (configurable; default $3.60)."""
    return _number(settings.get("pricing.tor_usd")) or 3.60


def _cache_seconds() -> float:
    return max(1.0, _number(settings.get("market.cache_seconds")) or 15.0)


def _sample_seconds() -> float:
    return max(5.0, _number(settings.get("market.sample_seconds")) or 60.0)


def _history_path():
    return data_dir("market") / "history.json"


def _history() -> dict[str, list]:
    doc = read_json(_history_path(), {}) or {}
    return doc if isinstance(doc, dict) else {}


def remember(samples: dict[str, float]) -> int:
    """Append one observation per symbol (throttled to ``market.sample_seconds``)."""
    now = time.time()
    with LOCK:
        if now - float(_LAST_SAMPLE.get("at") or 0) < _sample_seconds():
            return 0
        _LAST_SAMPLE["at"] = now
        history = _history()
        cap = int(_number(settings.get("market.history_points")) or 2000)
        stamp = int(now)
        written = 0
        for symbol, price in samples.items():
            if not price or price <= 0:
                continue
            series = history.setdefault(str(symbol).upper(), [])
            if series and int(series[-1][0]) == stamp:
                series[-1][1] = round(float(price), 12)
            else:
                series.append([stamp, round(float(price), 12)])
            if len(series) > cap:
                del series[:len(series) - cap]
            written += 1
        if written:
            write_json(_history_path(), history)
        return written


def _change_24h(symbol: str, price: float) -> tuple[float | None, int]:
    series = _history().get(symbol.upper()) or []
    if not series:
        return None, 0
    cutoff = int(time.time()) - 86400
    window = [point for point in series if int(point[0]) >= cutoff]
    if len(window) < 2:
        return None, len(window)
    first = _number(window[0][1])
    if first <= 0:
        return None, len(window)
    return round((price - first) / first * 100.0, 4), len(window)


def _drift(tokens: list[dict], books: list[dict], anchor: float) -> dict:
    """Turnover-based drift for TOR, clamped to ±15% of the anchor."""
    volume = sum(_number(row.get("volume_24h_tor")) for row in tokens) * anchor
    pooled = sum(_number(row.get("liquidity_tor")) for row in tokens) * anchor
    pooled += sum(_number(book.get("liquidity_usd")) for book in books)
    if pooled <= 0:
        return {"ratio": 0.0, "drift": 0.0, "volume_usd": round(volume, 2),
                "liquidity_usd": 0.0, "rule": "no pooled liquidity — anchor held"}
    ratio = volume / pooled
    drift = max(-0.15, min(0.15, ratio * 0.35))
    return {"ratio": round(ratio, 8), "drift": round(drift, 8),
            "volume_usd": round(volume, 2), "liquidity_usd": round(pooled, 2),
            "rule": "0.35 × (24 h volume ÷ pooled liquidity), clamped to ±15%"}


def _row_view(row: dict, tor_usd: float, books: dict[str, dict]) -> dict:
    symbol = str(row.get("symbol") or "").upper()
    supply = int(row.get("supply") or 0)
    price_tor = row.get("price_tor")
    pools: list[dict] = []
    if price_tor:
        pools.append({"base": "TOR", "price_usd": round(_number(price_tor) * tor_usd, 12),
                      "liquidity_usd": round(_number(row.get("liquidity_tor")) * tor_usd, 2),
                      "price_tor": _number(price_tor),
                      "liquidity_tor": _number(row.get("liquidity_tor"))})
    book = books.get(symbol)
    if book and book.get("price_usd"):
        pools.append({"base": book.get("base") or "USDC",
                      "price_usd": _number(book.get("price_usd")),
                      "liquidity_usd": _number(book.get("liquidity_usd")),
                      "token_reserve": book.get("token_reserve"),
                      "base_reserve": book.get("usdc_reserve")})
    pools.sort(key=lambda item: _number(item.get("liquidity_usd")), reverse=True)
    price_usd = _number(pools[0]["price_usd"]) if pools else None
    liquidity_usd = round(sum(_number(item.get("liquidity_usd")) for item in pools), 2)
    return {**row,
            "price_usd": price_usd,
            "price_tor": (round(price_usd / tor_usd, 12) if price_usd and tor_usd else None),
            "market_cap_usd": (round(price_usd * supply, 2) if price_usd and supply else None),
            "liquidity_usd": liquidity_usd,
            "volume_24h_usd": round(_number(row.get("volume_24h_tor")) * tor_usd, 2),
            "quote_assets": [item["base"] for item in pools],
            "pools_usd": pools}



def snapshot(limit: int = 500, *, refresh: bool = False) -> dict:
    """One cached view of TOR plus every tradable token, in USD."""
    with LOCK:
        cached = _CACHE.get("payload")
        age = time.time() - float(_CACHE.get("at") or 0)
        if cached and not refresh and age < _cache_seconds():
            return dict(cached)                     # type: ignore[arg-type]
        table = token_registry.snapshot(min(2000, max(1, int(limit))))
        rows = list(table.get("tokens") or [])
        books = {str(book.get("symbol") or "").upper(): book
                 for book in pool_books.catalog()}
        anchor = anchor_usd()
        drift = _drift(rows, list(books.values()), anchor)
        feed = tor_price.quote(
            volume_tor=sum(_number(row.get("volume_24h_tor")) for row in rows),
            swaps=sum(_number(row.get("volume_24h_swaps")) for row in rows),
            liquidity_tor=sum(_number(row.get("liquidity_tor")) for row in rows),
            anchor_usd=anchor)
        tor_usd = feed["price_usd"]
        native = next((row for row in rows
                       if str(row.get("symbol")).upper() == "TOR"), None)
        views = [_row_view(row, tor_usd, books) for row in rows
                 if row is not native]
        samples = {str(view["symbol"]): _number(view["price_tor"]) for view in views}
        samples["TOR"] = tor_usd
        remember(samples)
        for view in views:
            change, observations = _change_24h(str(view["symbol"]),
                                               _number(view.get("price_tor")))
            view["change_24h"] = change
            view["observations"] = observations
        tor_supply = int((native or {}).get("supply") or 0)
        tor_row = {
            "symbol": "TOR", "name": "OneNess TOR",
            "price_usd": tor_usd, "anchor_usd": anchor, "price_tor": 1.0,
            "supply": tor_supply,
            "market_cap_usd": round(tor_usd * tor_supply, 2) if tor_supply else None,
            "liquidity_usd": round(sum(_number(row.get("liquidity_tor")) for row in rows)
                                   * tor_usd, 2),
            "volume_24h_usd": drift["volume_usd"],
            "volume_24h_tor": round(drift["volume_usd"] / tor_usd, 8) if tor_usd else 0.0,
            "holders": (native or {}).get("holders"),
            "image": (native or {}).get("image"),
            "mint_address": (native or {}).get("mint_address"),
            "change_24h": _change_24h("TOR", tor_usd)[0],
            "price_source": feed["source"],
            "update_seconds": feed["update_seconds"],
            "updated_at": feed["updated_at"],
            "next_update_at": feed["next_update_at"],
            "floor_usd": feed["floor_usd"], "cap_usd": feed["cap_usd"],
            "volatility": {key: feed[key] for key in
                           ("standard", "tick", "step_usd", "direction", "bias",
                            "target_usd", "clamped", "rule", "engine_fresh")},
            "activity": feed["activity"],
            "price_history": feed["history"],
            "drift": drift, "quote": "USD",
        }
        totals = {
            "tokens": len(views),
            "market_cap_usd": round(sum(_number(view.get("market_cap_usd"))
                                        for view in views), 2),
            "liquidity_usd": round(sum(_number(view.get("liquidity_usd"))
                                       for view in views), 2),
            "volume_24h_usd": round(sum(_number(view.get("volume_24h_usd"))
                                        for view in views), 2),
            "tor_pools": sum(1 for view in views if "TOR" in view["quote_assets"]),
            "usdc_pools": sum(1 for view in views if "USDC" in view["quote_assets"]),
            "tradable": sum(1 for view in views if view.get("price_usd")),
        }
        payload = {
            "standard": STANDARD, "at": _now(),
            "tor": tor_row, "tokens": views, "totals": totals,
            "books": pool_books.catalog(),
            "usdc_custody": pool_books.custody_balances(),
            "sources": {
                "prices": "TOR pools on the Rust ledger + USDC books on this node",
                "supply": "on-chain token supply",
                "volume": "real swap records the ledger holds",
                "tor_usd": tor_price.RULE,
            },
        }
        _CACHE["payload"] = payload
        _CACHE["at"] = time.time()
        return dict(payload)


def forget() -> None:
    """Drop the cached board (a published quote must be visible immediately)."""
    with LOCK:
        _CACHE["payload"] = None
        _CACHE["at"] = 0.0


def publish_tor_price(payload: dict, *, actor: str = "") -> dict:
    """Accept the Price Engine's TOR/USD quote and drop the stale board."""
    block = tor_price.publish(payload.get("price_usd"),
                              activity=payload.get("activity")
                              if isinstance(payload.get("activity"), dict) else None,
                              actor=actor or str(payload.get("source") or "engine"),
                              source="engine")
    forget()
    return block




def token(symbol: object, *, refresh: bool = False) -> dict:
    """One token's market row (TOR included).

    A token minted a second ago is not in the 15-second snapshot yet, so a miss
    re-reads the ledger once before answering ``NotFound`` — the caller never
    sees a 404 for a token that exists.
    """
    wanted = str(symbol or "").strip().upper()
    payload = snapshot(refresh=refresh)
    if wanted == "TOR":
        return payload["tor"]
    row = next((item for item in payload["tokens"]
                if str(item.get("symbol")).upper() == wanted), None)
    if row is None and not refresh:
        return token(wanted, refresh=True)
    if row is None:
        from core.errors import NotFound

        raise NotFound(f"no market row for {wanted}",
                       details={"known": [item["symbol"]
                                          for item in payload["tokens"]][:40]})
    return row


def chart(symbol: object, points: int | None = None) -> dict:
    """Candles from real sampled prices — flat (and honest) before any trade."""
    ticker = str(symbol or "").strip().upper() or "TOR"
    wanted = int(_number(points or settings.get("market.chart_points")) or 168)
    wanted = max(2, min(1000, wanted))
    series = list(_history().get(ticker) or [])
    current = (_number(token(ticker).get("price_usd")) if ticker == "TOR"
               else _number(token(ticker).get("price_tor")))
    if not series:
        series = [[int(time.time()), current]]
    series = series[-wanted:]
    candles = [{"time": int(stamp), "open": _number(price), "high": _number(price),
                "low": _number(price), "close": _number(price)}
               for stamp, price in series]
    closes = [_number(point[1]) for point in series]
    first, last = closes[0], closes[-1]
    return {"symbol": ticker, "interval": "sample", "points": len(candles),
            "candles": candles,
            "summary": {"first": first, "last": last, "high": max(closes),
                        "low": min(closes),
                        "change_percent": (round((last - first) / first * 100.0, 4)
                                           if first else None)},
            "source": ("live AMM samples" if len(series) > 1
                       else "one observation — flat at the pool price"),
            "span": len(series)}


def overview() -> dict:
    """Everything a dashboard needs in one call: TOR, totals, movers, pools."""
    payload = snapshot()
    tokens = [row for row in payload["tokens"] if row.get("price_usd")]
    gainers = sorted(tokens, key=lambda row: _number(row.get("change_24h")),
                     reverse=True)[:6]
    losers = sorted(tokens, key=lambda row: _number(row.get("change_24h")))[:6]
    newest = sorted(tokens, key=lambda row: int(row.get("created_at") or 0),
                    reverse=True)[:8]
    deepest = sorted(tokens, key=lambda row: _number(row.get("liquidity_usd")),
                     reverse=True)[:8]
    return {"standard": STANDARD, "at": payload["at"], "tor": payload["tor"],
            "totals": payload["totals"],
            "pools": {"tor": payload["totals"]["tor_pools"],
                      "usdc": payload["totals"]["usdc_pools"],
                      "books": payload["books"][:12]},
            "movers": {"gainers": gainers, "losers": losers},
            "newest": newest, "deepest": deepest,
            "sources": payload["sources"]}

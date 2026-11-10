"""The 120-asset market universe: the top 100 by market cap plus the top 20
Solana memecoins — each with a price, a supply and one canonical logo.

Three independent, free, key-less providers answer the same question, so one
provider having a bad day never empties the table:

======================  =============================  ==========================
list                    primary                        fallbacks (in this order)
======================  =============================  ==========================
top 100 assets          CoinGecko ``/coins/markets``    CoinPaprika ``/v1/tickers``
                                                       CoinLore ``/api/tickers``
top 20 Solana memecoins CoinGecko category              GeckoTerminal Solana pools
                        ``solana-meme-coins``          DexScreener token profiles
======================  =============================  ==========================

What a row means — and what it does not:

* ``price_usd`` / ``market_cap_usd`` / ``supply`` are **public market data**,
  quoted in USD by the provider. They describe the real asset a ticker refers
  to; they are not chain state, and this node quotes no fiat rate of its own
  (:mod:`oneness_tor.services.token_registry` keeps every on-chain price in TOR).
* ``supply`` is the provider's *total* supply when it publishes one, otherwise
  its circulating supply — ``supply_basis`` always says which.
* ``image`` is one URL per asset. When the winning provider publishes no logo
  the cached public metadata (:mod:`oneness_tor.services.token_media`) fills it,
  so the mint, the logo cache and every page show the same picture.
* ``source`` / ``source_id`` say exactly which provider answered and what its
  own id for that asset was — nothing is inferred, nothing is invented.
* The document is cached for ``tokens.universe.ttl_hours``; a failed refresh
  keeps the previous cache instead of blanking the table.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from core.config import settings
from oneness_tor.services.store import data_dir, read_json, write_json

#: How many assets each list holds — 100 + 20 = the 120-token fleet.
DEFAULT_MAJORS = 100
DEFAULT_MEMES = 20

#: Key-less providers. Every URL below answers without a token or an API key.
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
COINPAPRIKA_TICKERS = "https://api.coinpaprika.com/v1/tickers"
COINLORE_TICKERS = "https://api.coinlore.net/api/tickers/"
GECKOTERMINAL_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/pools"
DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
#: CoinGecko's own category id for "Solana meme coins".
MEME_CATEGORY = "solana-meme-coins"
#: DexScreener's batch limit for ``/latest/dex/tokens``.
MAX_ADDRESSES = 30

STORE = "data/metadata/market-universe.json"
STANDARD = "oneness-tor-market-universe/1"
USER_AGENT = "OneNessTOR/1.0 (+https://onenesstech.ca)"
MAJORS_LIST = "majors"
MEMES_LIST = "solana-memecoins"

#: The chain's ticker rule (``meme.SYMBOL_RE``) — kept local so this module
#: stays import-light and can be tested without the launcher.
TICKER_RE = re.compile(r"^[A-Z0-9]{2,10}$")

_LOCK = threading.RLock()
_ATTEMPT: dict = {"at": 0.0, "errors": []}


# --------------------------------------------------------------------- config
def _enabled() -> bool:
    return bool(settings.get("tokens.universe.enabled"))


def _ttl_hours() -> float:
    try:
        return max(0.0, float(settings.get("tokens.universe.ttl_hours") or 6))
    except (TypeError, ValueError):
        return 6.0


def _retry_seconds() -> float:
    try:
        return max(0.0, float(settings.get("tokens.universe.retry_seconds") or 300))
    except (TypeError, ValueError):
        return 300.0


def _timeout() -> float:
    try:
        return max(2.0, float(settings.get("tokens.universe.timeout") or 20.0))
    except (TypeError, ValueError):
        return 20.0


def _path():
    return data_dir("metadata") / "market-universe.json"


def _iso(stamp: float | int | None = None) -> str:
    moment = float(stamp if stamp is not None else time.time())
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------- HTTP
def _fetch_json(url: str, timeout: float | None = None) -> tuple[object | None, str | None]:
    """GET ``url`` → ``(payload, error)``; never raises, never hangs for long."""
    request = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout or _timeout()) as reply:
            body = reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code} from {url}"
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, f"{getattr(error, 'reason', error)} ({url})"
    try:
        return json.loads(body) if body else None, None
    except json.JSONDecodeError:
        return None, f"non-JSON answer from {url}"



# ------------------------------------------------------------------ normalise
def _number(value: object) -> float | None:
    """A finite, non-negative float — or ``None`` (never a guess)."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _ticker(value: object) -> str:
    return str(value or "").strip().upper()


def _name(value: object, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return (text or fallback)[:80]


def _row(*, symbol: object, name: object = "", list_name: str = MAJORS_LIST,
         rank: object = None, price: object = None, market_cap: object = None,
         supply: object = None, supply_basis: str = "", source: str = "",
         source_id: object = None, token_address: object = None,
         image: object = None, image_source: str = "") -> dict | None:
    """One normalised asset, or ``None`` when the ticker cannot exist on chain."""
    ticker = _ticker(symbol)
    if not TICKER_RE.match(ticker):
        return None
    amount = _number(supply)
    return {
        "symbol": ticker,
        "name": _name(name, ticker),
        "list": list_name,
        "rank": int(rank) if isinstance(rank, (int, float)) and rank else None,
        "price_usd": _number(price),
        "market_cap_usd": _number(market_cap),
        "supply": int(round(amount)) if amount else None,
        "supply_basis": supply_basis if amount else "",
        "source": source,
        "source_id": str(source_id) if source_id not in (None, "") else None,
        "token_address": str(token_address or "") or None,
        "image": str(image or "").strip(),
        "image_source": image_source if image else "",
    }


def _dedupe(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """First (best-ranked) row per ticker wins; the rest are reported."""
    kept: dict[str, dict] = {}
    dropped: list[str] = []
    for row in rows:
        if row["symbol"] in kept:
            dropped.append(row["symbol"])
            continue
        kept[row["symbol"]] = row
    return list(kept.values()), dropped


# ------------------------------------------------------------ providers: top 100
def _majors_coingecko(limit: int, timeout: float) -> tuple[list[dict], str | None]:
    """CoinGecko ``/coins/markets`` — price, market cap, supply and a logo."""
    query = urllib.parse.urlencode({"vs_currency": "usd", "order": "market_cap_desc",
                                    "per_page": max(1, min(250, limit)),
                                    "page": 1, "sparkline": "false"})
    payload, error = _fetch_json(f"{COINGECKO_MARKETS}?{query}", timeout)
    if not isinstance(payload, list) or not payload:
        return [], error or "CoinGecko returned no rows"
    rows: list[dict] = []
    for position, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        total = _number(item.get("total_supply"))
        row = _row(symbol=item.get("symbol"), name=item.get("name"),
                   list_name=MAJORS_LIST,
                   rank=item.get("market_cap_rank") or position,
                   price=item.get("current_price"),
                   market_cap=item.get("market_cap"),
                   supply=total if total else item.get("circulating_supply"),
                   supply_basis="total" if total else "circulating",
                   source="coingecko", source_id=item.get("id"),
                   image=item.get("image"),
                   image_source="coingecko" if item.get("image") else "")
        if row:
            rows.append(row)
    return rows[:limit], None


def _majors_coinpaprika(limit: int, timeout: float) -> tuple[list[dict], str | None]:
    """CoinPaprika ``/v1/tickers`` — first fallback (no logos, real supply)."""
    payload, error = _fetch_json(f"{COINPAPRIKA_TICKERS}?quotes=USD", timeout)
    if not isinstance(payload, list) or not payload:
        return [], error or "CoinPaprika returned no rows"
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("rank"):
            continue
        quote = (item.get("quotes") or {}).get("USD") or {}
        total = _number(item.get("total_supply"))
        row = _row(symbol=item.get("symbol"), name=item.get("name"),
                   list_name=MAJORS_LIST, rank=item.get("rank"),
                   price=quote.get("price"), market_cap=quote.get("market_cap"),
                   supply=total if total else item.get("max_supply"),
                   supply_basis="total" if total else "maximum",
                   source="coinpaprika", source_id=item.get("id"))
        if row:
            rows.append(row)
    rows.sort(key=lambda row: (row["rank"] or 10 ** 6, row["symbol"]))
    return rows[:limit], None


def _majors_coinlore(limit: int, timeout: float) -> tuple[list[dict], str | None]:
    """CoinLore ``/api/tickers`` — second fallback (strings, no logos)."""
    payload, error = _fetch_json(
        f"{COINLORE_TICKERS}?start=0&limit={max(1, min(100, limit))}", timeout)
    rows_in = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows_in, list) or not rows_in:
        return [], error or "CoinLore returned no rows"
    rows: list[dict] = []
    for item in rows_in:
        if not isinstance(item, dict):
            continue
        total = _number(item.get("tsupply")) or _number(item.get("msupply"))
        row = _row(symbol=item.get("symbol"), name=item.get("name"),
                   list_name=MAJORS_LIST, rank=_number(item.get("rank")),
                   price=item.get("price_usd"),
                   market_cap=item.get("market_cap_usd"),
                   supply=total if total else item.get("csupply"),
                   supply_basis="total" if total else "circulating",
                   source="coinlore", source_id=item.get("id"))
        if row:
            rows.append(row)
    rows.sort(key=lambda row: (row["rank"] or 10 ** 6, row["symbol"]))
    return rows[:limit], None


# --------------------------------------------------------- providers: memecoins
def _memes_coingecko(limit: int, timeout: float) -> tuple[list[dict], str | None]:
    """CoinGecko's own ``solana-meme-coins`` category — the primary list."""
    query = urllib.parse.urlencode({"vs_currency": "usd", "order": "market_cap_desc",
                                    "per_page": max(1, min(250, limit)),
                                    "page": 1, "sparkline": "false",
                                    "category": MEME_CATEGORY})
    payload, error = _fetch_json(f"{COINGECKO_MARKETS}?{query}", timeout)
    if not isinstance(payload, list) or not payload:
        return [], error or "CoinGecko returned no memecoin rows"
    rows: list[dict] = []
    for position, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        total = _number(item.get("total_supply"))
        row = _row(symbol=item.get("symbol"), name=item.get("name"),
                   list_name=MEMES_LIST,
                   rank=item.get("market_cap_rank") or position,
                   price=item.get("current_price"),
                   market_cap=item.get("market_cap"),
                   supply=total if total else item.get("circulating_supply"),
                   supply_basis="total" if total else "circulating",
                   source="coingecko", source_id=item.get("id"),
                   image=item.get("image"),
                   image_source="coingecko" if item.get("image") else "")
        if row:
            rows.append(row)
    return rows[:limit], None


def _address_meta(addresses: list, timeout: float) -> dict:
    """``{mint: {symbol, name, image, market_cap}}`` from one DexScreener batch."""
    wanted = [str(item).strip() for item in addresses if str(item or "").strip()]
    if not wanted:
        return {}
    payload, _error = _fetch_json(
        f"{DEXSCREENER_TOKENS}/{','.join(wanted[:MAX_ADDRESSES])}", timeout)
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    meta: dict = {}
    for pair in pairs or []:
        if not isinstance(pair, dict):
            continue
        base = pair.get("baseToken") or {}
        address = str(base.get("address") or "").strip()
        if not address:
            continue
        cap = _number(pair.get("marketCap")) or _number(pair.get("fdv")) or 0.0
        previous = meta.get(address)
        if previous and (previous.get("market_cap") or 0.0) >= cap:
            continue
        meta[address] = {"symbol": str(base.get("symbol") or "").strip(),
                         "name": str(base.get("name") or "").strip(),
                         "image": str((pair.get("info") or {}).get("imageUrl")
                                      or "").strip(),
                         "market_cap": cap}
    return meta


def _memes_geckoterminal(limit: int, timeout: float) -> tuple[list[dict], str | None]:
    """GeckoTerminal's busiest Solana pools → one row per token (fallback 1).

    GeckoTerminal ranks the pools (market cap, price) but no longer inlines the
    token metadata, so the mint addresses are resolved through one batched
    DexScreener call; if that is unavailable the pool name's base symbol is used
    and the logo comes from the cached public metadata.
    """
    query = urllib.parse.urlencode({"page": 1, "sort": "h24_volume_usd_desc"})
    payload, error = _fetch_json(f"{GECKOTERMINAL_POOLS}?{query}", timeout)
    pools = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(pools, list) or not pools:
        return [], error or "GeckoTerminal returned no pools"
    candidates: list[dict] = []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        attributes = pool.get("attributes") or {}
        link = (((pool.get("relationships") or {}).get("base_token") or {})
                .get("data") or {})
        address = str(link.get("id") or "").replace("solana_", "").strip()
        quoted = str(attributes.get("name") or "").split("/")[0].strip()
        candidates.append({
            "address": address,
            "price": attributes.get("base_token_price_usd"),
            "market_cap": (_number(attributes.get("market_cap_usd"))
                           or _number(attributes.get("fdv_usd"))),
            "symbol": quoted,
        })
    if not candidates:
        return [], "GeckoTerminal returned no pools"
    meta = _address_meta([item["address"] for item in candidates], timeout)
    best: dict[str, dict] = {}
    for item in candidates:
        found = meta.get(item["address"]) or {}
        symbol = found.get("symbol") or item["symbol"]
        row = _row(symbol=symbol, name=found.get("name") or symbol,
                   list_name=MEMES_LIST, price=item["price"],
                   market_cap=(item["market_cap"] or found.get("market_cap")),
                   supply=None, source="geckoterminal",
                   source_id=item["address"] or None,
                   token_address=item["address"], image=found.get("image") or "",
                   image_source="dexscreener" if found.get("image") else "")
        if not row:
            continue
        previous = best.get(row["symbol"])
        if not previous or (row["market_cap_usd"] or 0) > (previous["market_cap_usd"] or 0):
            best[row["symbol"]] = row
    rows = sorted(best.values(), key=lambda row: row["market_cap_usd"] or 0,
                  reverse=True)
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
    return rows[:limit], None


def _memes_dexscreener(limit: int, timeout: float) -> tuple[list[dict], str | None]:
    """DexScreener's latest Solana token profiles + one batched metrics call."""
    payload, error = _fetch_json(DEXSCREENER_PROFILES, timeout)
    if not isinstance(payload, list) or not payload:
        return [], error or "DexScreener returned no token profiles"
    icons: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict) or item.get("chainId") != "solana":
            continue
        address = str(item.get("tokenAddress") or "").strip()
        if address:
            icons.setdefault(address, str(item.get("icon") or "").strip())
    addresses = list(icons)[:MAX_ADDRESSES]
    if not addresses:
        return [], "DexScreener returned no Solana token profiles"
    batch, error = _fetch_json(f"{DEXSCREENER_TOKENS}/{','.join(addresses)}", timeout)
    pairs = batch.get("pairs") if isinstance(batch, dict) else None
    if not isinstance(pairs, list) or not pairs:
        return [], error or "DexScreener returned no pairs"
    best: dict[str, dict] = {}
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken") or {}
        address = str(base.get("address") or "").strip()
        icon = str((pair.get("info") or {}).get("imageUrl")
                   or icons.get(address) or "").strip()
        row = _row(symbol=base.get("symbol"), name=base.get("name"),
                   list_name=MEMES_LIST, price=pair.get("priceUsd"),
                   market_cap=_number(pair.get("marketCap")) or _number(pair.get("fdv")),
                   supply=None, source="dexscreener", source_id=address or None,
                   token_address=address, image=icon,
                   image_source="dexscreener" if icon else "")
        if not row:
            continue
        previous = best.get(row["symbol"])
        if not previous or (row["market_cap_usd"] or 0) > (previous["market_cap_usd"] or 0):
            best[row["symbol"]] = row
    rows = sorted(best.values(), key=lambda row: row["market_cap_usd"] or 0,
                  reverse=True)
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
    return rows[:limit], None


# --------------------------------------------------------------- the fallbacks
MAJOR_PROVIDERS = (("coingecko", _majors_coingecko),
                   ("coinpaprika", _majors_coinpaprika),
                   ("coinlore", _majors_coinlore))
MEME_PROVIDERS = (("coingecko", _memes_coingecko),
                  ("geckoterminal", _memes_geckoterminal),
                  ("dexscreener", _memes_dexscreener))


def _attach_images(rows: list[dict]) -> int:
    """Fill a missing logo from the cached public metadata — one picture per asset."""
    from oneness_tor.services import token_media

    filled = 0
    for row in rows:
        if row["image"]:
            continue
        entry = token_media.lookup(row["symbol"])
        image = str(entry.get("image") or "").strip()
        if not image:
            continue
        row["image"] = image
        row["image_source"] = (f"token-media cache "
                               f"({entry.get('source') or 'public metadata'})")
        filled += 1
    return filled


def _carry_missing(rows: list[dict], previous: dict) -> int:
    """Keep a supply or logo we already knew when the new answer omits it.

    A fallback provider (CoinPaprika, CoinLore, GeckoTerminal, DexScreener) often
    publishes no total supply and no logo for the same asset the primary knows, so
    a refresh that had to fall back would otherwise *lose* real data. The fields
    are copied per ticker (same asset), and the carried row says where they came
    from. Nothing is computed: an unknown supply stays ``None``.
    """
    filled = 0
    for row in rows:
        old = previous.get(row["symbol"]) or {}
        if not row.get("supply") and old.get("supply"):
            row["supply"] = old["supply"]
            row["supply_basis"] = old.get("supply_basis") or ""
            row["supply_source"] = old.get("source")
            filled += 1
        if not row.get("image") and old.get("image"):
            row["image"] = old["image"]
            row["image_source"] = (f"previous feed snapshot "
                                   f"({old.get('source') or 'public metadata'})")
            filled += 1
    return filled


def _pick(label: str, providers: tuple, limit: int,
          timeout: float) -> tuple[list[dict], list[dict]]:
    """Walk the fallback chain; return the first answer plus every attempt."""
    attempts: list[dict] = []
    for name, provider in providers:
        rows, error = provider(limit, timeout)
        attempts.append({"list": label, "source": name, "ok": bool(rows),
                         "rows": len(rows), "error": error})
        if rows:
            return rows, attempts
    return [], attempts


def _clamp(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _read_doc() -> dict:
    doc = read_json(_path(), {}) or {}
    return doc if isinstance(doc, dict) else {}


def _write_doc(doc: dict) -> None:
    write_json(_path(), doc)


def _stale(doc: dict) -> bool:
    fetched = float(doc.get("fetched_at_unix") or 0)
    return fetched <= 0 or (time.time() - fetched) > _ttl_hours() * 3600


def _may_attempt() -> bool:
    return (time.time() - float(_ATTEMPT.get("at") or 0.0)) >= _retry_seconds()


def rows_for(doc: dict, majors: object = DEFAULT_MAJORS,
             memes: object = DEFAULT_MEMES) -> list[dict]:
    """The first ``majors`` top-100 rows followed by the first ``memes`` memecoins."""
    wanted_majors = _clamp(majors, DEFAULT_MAJORS, 0, 250)
    wanted_memes = _clamp(memes, DEFAULT_MEMES, 0, 100)
    rows = [row for row in (doc.get("rows") or []) if isinstance(row, dict)]
    top = [row for row in rows if row.get("list") == MAJORS_LIST][:wanted_majors]
    meme_rows = [row for row in rows if row.get("list") == MEMES_LIST][:wanted_memes]
    return top + meme_rows


def universe(*, majors: object = DEFAULT_MAJORS, memes: object = DEFAULT_MEMES,
             refresh: bool = False) -> dict:
    """The cached 120-asset document, refreshed when stale (best effort).

    A failed refresh keeps the previous cache: a provider having a bad day never
    blanks the table, and every row still says which provider answered it.
    """
    wanted_majors = _clamp(majors, DEFAULT_MAJORS, 1, 250)
    wanted_memes = _clamp(memes, DEFAULT_MEMES, 0, 100)
    with _LOCK:
        doc = _read_doc()
        if _enabled() and (refresh or _stale(doc)) and _may_attempt():
            _ATTEMPT["at"] = time.time()
            timeout = _timeout()
            # over-fetch: a provider always returns a few tickers the chain cannot
            # hold (>10 chars, non-alphanumeric) or that both lists share
            fetch_majors = min(250, wanted_majors + 30)
            fetch_memes = min(250, wanted_memes + 30)
            major_rows, major_attempts = _pick(MAJORS_LIST, MAJOR_PROVIDERS,
                                               fetch_majors, timeout)
            meme_rows, meme_attempts = _pick(MEMES_LIST, MEME_PROVIDERS,
                                             fetch_memes, timeout)
            merged, duplicates = _dedupe(major_rows + meme_rows)
            errors = [f"{item['list']}/{item['source']}: {item['error']}"
                      for item in major_attempts + meme_attempts if item["error"]]
            _ATTEMPT["errors"] = errors
            if merged:
                previous, _before = _dedupe([row for row in (doc.get("rows") or [])
                                             if isinstance(row, dict)])
                carried = _carry_missing(merged, {row["symbol"]: row
                                                 for row in previous})
                filled = _attach_images(merged)
                cached = {"rows": merged}
                selected = rows_for(cached, wanted_majors, wanted_memes)
                doc = {
                    "standard": STANDARD, "store": STORE, "enabled": True,
                    "majors": {"requested": wanted_majors,
                               "returned": sum(1 for row in selected
                                               if row["list"] == MAJORS_LIST),
                               "source": next((item["source"] for item in major_attempts
                                               if item["ok"]), None),
                               "attempts": major_attempts},
                    "memes": {"requested": wanted_memes,
                              "returned": sum(1 for row in selected
                                              if row["list"] == MEMES_LIST),
                              "source": next((item["source"] for item in meme_attempts
                                              if item["ok"]), None),
                              "attempts": meme_attempts},
                    "rows": merged, "count": len(merged),
                    "images": {"with_image": sum(1 for row in selected if row["image"]),
                               "filled_from_cache": filled,
                               "carried_from_previous": carried,
                               "total": len(selected)},
                    "duplicates": sorted(set(duplicates)),
                    "fetched_at": _iso(), "fetched_at_unix": int(time.time()),
                    "ttl_hours": _ttl_hours(), "errors": errors,
                    "note": ("public market data (price, market cap, supply, logo) "
                             "from key-less providers, cached on this node; the same "
                             "logo URL is what the mint pins and every page draws"),
                }
                _write_doc(doc)
            else:
                doc = {**doc, "errors": errors, "last_attempt_at": _iso()}
                _write_doc(doc)
        fetched = float(doc.get("fetched_at_unix") or 0)
        age = int(time.time() - fetched) if fetched else None
        rows = rows_for(doc, wanted_majors, wanted_memes)
        cached = [row for row in (doc.get("rows") or []) if isinstance(row, dict)]
        return {**doc,
                "standard": STANDARD, "store": STORE, "enabled": _enabled(),
                "majors_requested": wanted_majors, "memes_requested": wanted_memes,
                "rows": rows, "count": len(rows),
                "cache": {"standard": doc.get("standard"), "count": len(cached),
                          "fetched_at": doc.get("fetched_at")},
                "fetched_at": doc.get("fetched_at"), "age_seconds": age,
                "ttl_hours": _ttl_hours(), "stale": _stale(doc),
                "errors": doc.get("errors") or [],
                "warning": ("no provider answered and no cache exists yet — call "
                            "POST /api/tokens/universe/refresh (admin) once the "
                            "network is reachable") if not rows else None}


def status() -> dict:
    """The cache's health, without ever fetching anything."""
    with _LOCK:
        doc = _read_doc()
    fetched = float(doc.get("fetched_at_unix") or 0)
    rows = [row for row in (doc.get("rows") or []) if isinstance(row, dict)]
    return {"standard": STANDARD, "store": STORE, "enabled": _enabled(),
            "count": len(rows),
            "majors": sum(1 for row in rows if row.get("list") == MAJORS_LIST),
            "memes": sum(1 for row in rows if row.get("list") == MEMES_LIST),
            "with_image": sum(1 for row in rows if row.get("image")),
            "fetched_at": doc.get("fetched_at"),
            "age_seconds": int(time.time() - fetched) if fetched else None,
            "ttl_hours": _ttl_hours(), "stale": _stale(doc),
            "sources": {"majors": ((doc.get("majors") or {}).get("attempts") or []),
                        "memes": ((doc.get("memes") or {}).get("attempts") or [])},
            "last_errors": doc.get("errors") or []}


def refresh() -> dict:
    """Force a fetch from the providers now (the admin button / post-deploy hook)."""
    doc = universe(refresh=True)
    return {"refreshed_at": _iso(), "count": doc["count"],
            "majors": doc.get("majors"), "memes": doc.get("memes"),
            "images": doc.get("images"), "errors": doc.get("errors") or [],
            "store": STORE}

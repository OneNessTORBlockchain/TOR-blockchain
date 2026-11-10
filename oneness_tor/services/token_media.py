"""Token logos for the token table — fetched from public metadata, then cached.

The chain itself has no image field: a token is a ticker, a supply and an owner
(``rust/torchain``), so a listing that shows a logo has to resolve one. This
module does exactly that, in a fixed order, and always says where a picture came
from:

1. ``data/metadata/token-logos/`` — the **mirror**: a copy of the picture served
   by this node at ``/assets/tokens/<SYMBOL>.<ext>``, so no page depends on
   CoinGecko being reachable (Tor, a firewall, a rate limit or a removed image
   can never blank a row again),
2. ``data/metadata/token-images.json`` — the metadata cache (24 h TTL),
3. CoinGecko ``/coins/markets`` — the top coins by market cap, one request,
4. DexScreener ``/latest/dex/search`` — per symbol, only for tickers CoinGecko
   does not know (opt out with ``tokens.images.dexscreener: false``),
5. the launcher's own image (kept for as long as the token exists),
6. nothing — the pages then draw a deterministic identicon.

Every mint path (the 120-token fleet and the launcher) calls :func:`remember`,
which also mirrors the picture, so a token minted later gets the same treatment
as the ones already on chain. A refresh mirrors what it learns; a run can be
repeated at any time with ``POST /api/tokens/images/mirror`` (admin) or
``python tools/mirror_logos.py``.

Nothing is invented: an unknown symbol gets no image, every row carries
``image_source``, and a failed download keeps the previous cache instead of
blanking the table. Prices stay denominated in TOR; this module never quotes a
fiat value.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from core import coin, ipfs
from core.config import settings
from oneness_tor.services.store import data_dir, read_json, write_json

#: Bulk metadata: one request covers the top ``per_page`` coins by market cap.
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
#: Per-symbol fallback for tickers CoinGecko does not list.
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"

STORE = "data/metadata/token-images.json"
STANDARD = "oneness-tor-token-images/1"
#: The mirror: one file per ticker, served by this node itself.
LOGO_DIR = "data/metadata/token-logos"
LOGO_ROUTE = "/assets/tokens"
STANDARD_LOGOS = "oneness-tor-token-logos/1"
#: content-type → file extension. Anything else is refused, never guessed.
LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
              "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg"}
USER_AGENT = "OneNessTOR/1.0 (+https://onenesstech.ca)"
NATIVE_SOURCE = "pinned IPFS logo (core.coin)"

_LOCK = threading.RLock()
_ATTEMPT = {"at": 0.0, "error": None}
_MISSES: dict[str, float] = {}


# --------------------------------------------------------------------- config
def _enabled() -> bool:
    return bool(settings.get("tokens.images.enabled"))


def _ttl_hours() -> float:
    try:
        return max(0.0, float(settings.get("tokens.images.ttl_hours") or 24))
    except (TypeError, ValueError):
        return 24.0


def _retry_seconds() -> float:
    try:
        return max(0.0, float(settings.get("tokens.images.retry_seconds") or 600))
    except (TypeError, ValueError):
        return 600.0


def _timeout() -> float:
    try:
        return max(1.0, float(settings.get("tokens.images.timeout") or 8.0))
    except (TypeError, ValueError):
        return 8.0


def _per_page() -> int:
    try:
        return max(1, min(250, int(settings.get("tokens.images.per_page") or 250)))
    except (TypeError, ValueError):
        return 250


def _dex_fallback() -> bool:
    return bool(settings.get("tokens.images.dexscreener"))


def _mirror_enabled() -> bool:
    return bool(settings.get("tokens.images.mirror"))


def _mirror_limit() -> int:
    try:
        return max(1, int(settings.get("tokens.images.mirror_limit") or 500))
    except (TypeError, ValueError):
        return 500


def _mirror_max_bytes() -> int:
    try:
        return max(1024, int(settings.get("tokens.images.mirror_max_bytes")
                              or 2_000_000))
    except (TypeError, ValueError):
        return 2_000_000


def logo_dir():
    """The mirror folder (created on demand) — ``data/metadata/token-logos``."""
    return data_dir("metadata", "token-logos")


def _path():
    return data_dir("metadata") / "token-images.json"


def _iso(stamp: float | int | None = None) -> str:
    moment = float(stamp if stamp is not None else time.time())
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------- HTTP
def _fetch_json(url: str) -> tuple[object | None, str | None]:
    """GET ``url`` → ``(payload, error)``; never raises, never hangs for long."""
    request = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as reply:
            body = reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code} from {url}"
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, f"{getattr(error, 'reason', error)} ({url})"
    try:
        return json.loads(body) if body else None, None
    except json.JSONDecodeError:
        return None, f"non-JSON answer from {url}"


def _fetch_bytes(url: str) -> tuple[bytes | None, str, str | None]:
    """GET an image → ``(body, content_type, error)``; never raises, never huge."""
    request = urllib.request.Request(url, headers={
        "accept": "image/*", "user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as reply:
            content_type = str(reply.headers.get("content-type") or "")
            content_type = content_type.split(";")[0].strip().lower()
            body = reply.read(_mirror_max_bytes() + 1)
    except urllib.error.HTTPError as error:
        return None, "", f"HTTP {error.code} from {url}"
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, "", f"{getattr(error, 'reason', error)} ({url})"
    if not body:
        return None, content_type, f"empty answer from {url}"
    if len(body) > _mirror_max_bytes():
        return None, content_type, (f"{url} is larger than {_mirror_max_bytes()} "
                                    f"bytes — not mirrored")
    return body, content_type, None


def _entry(image: object, source: str, *, rank: object = None,
           coin_id: object = None, name: object = "") -> dict:
    url = str(image or "").strip()
    return {"image": url, "source": source,
            "rank": int(rank) if isinstance(rank, (int, float)) else None,
            "coingecko_id": str(coin_id or "") or None,
            "name": str(name or "") or None}


# -------------------------------------------------------------------- sources
def _from_coingecko() -> tuple[dict[str, dict], str | None]:
    """Top ``per_page`` coins by market cap → ``{SYMBOL: entry}``."""
    query = urllib.parse.urlencode({"vs_currency": "usd", "order": "market_cap_desc",
                                    "per_page": _per_page(), "page": 1,
                                    "sparkline": "false"})
    payload, error = _fetch_json(f"{COINGECKO_MARKETS}?{query}")
    rows = payload if isinstance(payload, list) else []
    if not rows:
        return {}, error or "CoinGecko returned no rows"
    images: dict[str, dict] = {}
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        image = row.get("image")
        if not symbol or not image or symbol in images:
            continue           # the first (highest market cap) coin keeps the ticker
        images[symbol] = _entry(image, "coingecko",
                                rank=row.get("market_cap_rank") or position,
                                coin_id=row.get("id"), name=row.get("name"))
    return images, None


def _from_dexscreener(symbol: str) -> dict | None:
    """Best-liquidity pair's logo for one ticker (``None`` when unknown)."""
    query = urllib.parse.urlencode({"q": symbol})
    payload, _error = _fetch_json(f"{DEXSCREENER_SEARCH}?{query}")
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list) or not pairs:
        return None

    def liquidity(pair: dict) -> float:
        return float(((pair or {}).get("liquidity") or {}).get("usd") or 0.0)

    best = max((pair for pair in pairs if isinstance(pair, dict)),
               key=liquidity, default=None)
    image = ((best or {}).get("info") or {}).get("imageUrl")
    if not image:
        return None
    base = (best or {}).get("baseToken") or {}
    return _entry(image, "dexscreener", name=base.get("name"))


# --------------------------------------------------------------------- cache
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


# ---------------------------------------------------------------------- mirror
# The chain has no image field and a third party is not a dependency this node
# can promise: every logo it resolves is copied into ``data/metadata/token-logos``
# and served from ``/assets/tokens/<SYMBOL>.<ext>``. The pages then draw a local
# file, so a Tor visitor, an offline box, a rate limit or a picture CoinGecko
# removed later can never blank a row again.
def _logo_file(entry: dict) -> str:
    """The mirrored file name for a cache entry (``''`` when not on disk)."""
    name = str((entry or {}).get("logo_file") or "").strip()
    return name if name and (logo_dir() / name).is_file() else ""


def _served_logo(entry: dict) -> str:
    """The URL this node serves a mirrored logo from (``''`` when absent)."""
    name = _logo_file(entry)
    return f"{LOGO_ROUTE}/{urllib.parse.quote(name)}" if name else ""


def _mirror_url(url: str) -> str:
    """The fetchable form of a logo URI: an ``ipfs://`` pin goes through a gateway."""
    text = str(url or "").strip()
    return ipfs.resolve(text) if text.lower().startswith("ipfs://") else text


def _write_logo(name: str, body: bytes):
    """Write one mirror file atomically — a reader never sees half a picture."""
    path = logo_dir() / name
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(body)
    os.replace(temporary, path)
    return path


def _mirror_doc(images: dict) -> dict:
    """What the mirror holds right now, without downloading anything."""
    rows = [row for row in images.values() if isinstance(row, dict)]
    return {"standard": STANDARD_LOGOS, "dir": LOGO_DIR, "route": LOGO_ROUTE,
            "count": sum(1 for row in rows if row.get("logo_file")),
            "total": len(images),
            "bytes": sum(int(row.get("logo_bytes") or 0) for row in rows),
            "at": max((str(row.get("logo_at") or "") for row in rows), default="")}


def _mirror_entries(symbols: list[str], *, force: bool = False,
                    limit: int | None = None) -> dict:
    """Copy the given symbols' pictures from the cache into the node's mirror."""
    with _LOCK:
        doc = _read_doc()
    images = doc.get("images") if isinstance(doc.get("images"), dict) else {}
    report = {"standard": STANDARD_LOGOS, "store": STORE, "enabled": True,
              "mirrored": 0, "skipped": 0, "failed": [], "bytes": 0,
              "capped": False}
    updated: dict[str, dict] = {}
    for ticker in sorted({str(item or "").strip().upper() for item in symbols}):
        if not ticker:
            continue
        if limit is not None and report["mirrored"] >= int(limit):
            report["capped"] = True
            break
        entry = images.get(ticker)
        entry = dict(entry) if isinstance(entry, dict) else {}
        url = str(entry.get("image") or "").strip()
        if not url:
            report["skipped"] += 1
            continue
        if not force and _logo_file(entry):
            report["skipped"] += 1          # already served by this node
            continue
        body, content_type, error = _fetch_bytes(_mirror_url(url))
        if error or not body:
            report["failed"].append({"symbol": ticker,
                                     "error": error or "no bytes returned"})
            continue
        extension = LOGO_TYPES.get(content_type)
        if not extension:
            report["failed"].append(
                {"symbol": ticker,
                 "error": f"unsupported content-type {content_type or 'unknown'!r}"})
            continue
        name = f"{ticker}.{extension}"
        try:
            _write_logo(name, body)
        except OSError as problem:
            report["failed"].append({"symbol": ticker, "error": str(problem)})
            continue
        previous = str(entry.get("logo_file") or "")
        if previous and previous != name:
            leftover = logo_dir() / previous
            if leftover.is_file():
                leftover.unlink(missing_ok=True)   # the picture changed format
        entry.update(logo_file=name, logo_bytes=len(body),
                     logo_sha256=hashlib.sha256(body).hexdigest(),
                     logo_type=content_type, logo_at=_iso())
        images[ticker] = entry
        updated[ticker] = entry
        report["mirrored"] += 1
        report["bytes"] += len(body)
    if updated:
        with _LOCK:
            current = _read_doc()
            stored = current.get("images")
            stored = dict(stored) if isinstance(stored, dict) else {}
            stored.update(updated)
            images = stored
            mirror_doc = {**_mirror_doc(stored), "at": _iso()}
            current.update(images=stored, mirror=mirror_doc, standard=STANDARD,
                           store=STORE)
            _write_doc(current)
    summary = _mirror_doc(images)
    report.update({key: summary[key] for key in
                   ("standard", "dir", "route", "count", "total", "at")})
    return report


def mirror(symbols: object = None, *, force: bool = False,
           limit: object = None) -> dict:
    """Copy logos onto this node (``data/metadata/token-logos``) and report it.

    Every mint path calls this through :func:`remember`, so a token created
    later is treated exactly like the ones already on chain. An entry that is
    already mirrored is skipped unless ``force`` is set; ``limit`` caps how many
    pictures one run downloads.
    """
    if isinstance(symbols, str):
        symbols = [part for part in symbols.replace(";", ",").split(",")
                   if part.strip()]
    wanted = ([str(item).strip().upper() for item in symbols]
              if isinstance(symbols, (list, tuple, set)) else None)
    try:
        budget = max(1, int(limit)) if limit is not None else _mirror_limit()
    except (TypeError, ValueError):
        budget = _mirror_limit()
    with _LOCK:
        doc = _read_doc()
    images = doc.get("images") if isinstance(doc.get("images"), dict) else {}
    if not (_enabled() and _mirror_enabled()):
        return {**_mirror_doc(images), "store": STORE, "enabled": False,
                "mirrored": 0, "skipped": 0, "failed": [], "bytes": 0,
                "note": ("logo mirroring is off (tokens.images.enabled / "
                         "tokens.images.mirror)")}
    return _mirror_entries(wanted if wanted is not None else sorted(images),
                           force=force, limit=budget)


def mirror_in_background(limit: object = None) -> dict:
    """Mirror whatever is missing without holding up the boot (best effort).

    A fresh clone, a new deployment or a node that has been offline for a while
    finds an empty ``data/metadata/token-logos``; this copies the pictures the
    cache already knows in a daemon thread, so the first page a visitor opens is
    served in the meantime and the mirror fills in behind it. Never raises.
    """
    report = {"standard": STANDARD_LOGOS, "route": LOGO_ROUTE, "dir": LOGO_DIR,
              "started": False}
    try:
        if not (_enabled() and _mirror_enabled()):
            report["note"] = "logo mirroring is off"
            return report
        if _mirror_doc(_read_doc().get("images") or {})["total"] == 0:
            report["note"] = "the logo cache is empty — nothing to mirror yet"
            return report
        worker = threading.Thread(target=mirror, kwargs={"limit": limit},
                                  name="token-logo-mirror", daemon=True)
        worker.start()
        report["started"] = True
    except (OSError, RuntimeError) as problem:      # a mirror never blocks a boot
        report["note"] = str(problem)
    return report


def catalog(*, refresh: bool = False) -> dict:
    """The cached ``symbol → logo`` map, refreshed when stale (best effort)."""
    with _LOCK:
        doc = _read_doc()
        images = doc.get("images") if isinstance(doc.get("images"), dict) else {}
        error = _ATTEMPT.get("error")
        if _enabled() and (refresh or _stale(doc)) and _may_attempt():
            _ATTEMPT["at"] = time.time()
            fresh, error = _from_coingecko()
            _ATTEMPT["error"] = error
            if fresh:
                images = {**images, **fresh}
                doc = {"standard": STANDARD, "source": "coingecko",
                       "store": STORE, "images": images,
                       "fetched_at": _iso(), "fetched_at_unix": int(time.time()),
                       "per_page": _per_page(),
                       "mirror": doc.get("mirror") or _mirror_doc(images),
                       "note": ("CoinGecko market metadata, cached on this node — "
                                "refresh with POST /api/tokens/images/refresh")}
                _write_doc(doc)
        fetched = float(doc.get("fetched_at_unix") or 0)
        age = int(time.time() - fetched) if fetched else None
        return {"standard": STANDARD,
                "source": str(doc.get("source") or "coingecko"),
                "store": STORE, "images": images, "count": len(images),
                "fetched_at": doc.get("fetched_at"),
                "age_seconds": age, "ttl_hours": _ttl_hours(),
                "stale": _stale(doc), "enabled": _enabled(),
                "dex_fallback": _dex_fallback(), "last_error": error,
                "logos": {**_mirror_doc(images),
                          "enabled": _enabled() and _mirror_enabled()},
                "note": ("logos are public metadata, not chain state; each one is "
                         "mirrored onto this node and served from " + LOGO_ROUTE +
                         ", and a symbol no source knows gets no image — the page "
                         "draws an identicon instead")}


def lookup(symbol: object) -> dict:
    """One token's logo: the native coin, the cache, or DexScreener."""
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        return {}
    if ticker == coin.COIN_SYMBOL:
        return {"image": coin.logo_url(), "source": NATIVE_SOURCE, "rank": None,
                "coingecko_id": None, "name": coin.COIN_NAME}
    found = (catalog().get("images") or {}).get(ticker)
    if found:
        return found
    if not (_enabled() and _dex_fallback()):
        return {}
    if (time.time() - float(_MISSES.get(ticker) or 0.0)) < _ttl_hours() * 3600:
        return {}
    with _LOCK:
        _MISSES[ticker] = time.time()
    entry = _from_dexscreener(ticker)
    if entry:
        with _LOCK:
            doc = _read_doc()
            images = doc.get("images") if isinstance(doc.get("images"), dict) else {}
            images[ticker] = entry
            doc.update(images=images, standard=STANDARD, store=STORE,
                       source=str(doc.get("source") or "coingecko"),
                       fetched_at=doc.get("fetched_at") or _iso(),
                       fetched_at_unix=int(doc.get("fetched_at_unix") or time.time()))
            _write_doc(doc)
    return entry or {}


def served_logo(symbol: object) -> str:
    """The local route this node serves a token's mirrored logo from (``''`` none).

    ``remember``/``mirror`` copy every picture into ``data/metadata/token-logos``
    and the pages draw ``/assets/tokens/<SYMBOL>.<ext>``. A mint wants that same
    URL in its token document, so the metadata and the page can never disagree
    about which picture a token carries.
    """
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        return ""
    entry = (catalog().get("images") or {}).get(ticker)
    return _served_logo(entry if isinstance(entry, dict) else {})


def decorate(rows: list[dict], *, launcher_images: dict | None = None) -> list[dict]:
    """Add ``image`` / ``image_source`` / ``image_rank`` to token rows in place.

    The node's own mirror wins (``/assets/tokens/<SYMBOL>.<ext>``, so the page
    never asks a third party for a picture); then a launcher image (supplied by
    :mod:`oneness_tor.services.meme`); the native
    coin always carries its pinned IPFS logo.
    """
    launchers = {str(key).upper(): str(value)
                 for key, value in (launcher_images or {}).items() if value}
    # read the cache once: a 100-token table must not re-read the file per row
    cached = catalog().get("images") or {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("symbol") or "").strip().upper()
        declared = str(row.get("image") or "").strip()
        entry = cached.get(ticker)
        entry = entry if isinstance(entry, dict) else {}
        served = _served_logo(entry) if ticker != coin.COIN_SYMBOL else ""
        if served:
            # the raw URI is kept for provenance; <img src> gets the local file
            row.setdefault("image_uri", declared or launchers.get(ticker)
                           or str(entry.get("image") or ""))
            row["image"] = served
            source = str(entry.get("source") or "public metadata")
            row["image_source"] = f"node logo store ({source}, mirrored)"
            row.setdefault("image_rank", entry.get("rank"))
            continue
        if declared:
            row.setdefault("image_uri", declared)
            row["image"] = ipfs.resolve(declared)
            row.setdefault("image_source", "launcher image")
            row.setdefault("image_rank", None)
            continue
        launcher = launchers.get(ticker)
        if launcher and ticker != coin.COIN_SYMBOL:
            row.update(image=ipfs.resolve(launcher), image_uri=launcher,
                       image_source="launcher image",
                       image_rank=None)
            continue
        entry = entry or lookup(ticker)
        row.update(image=entry.get("image", ""),
                   image_source=entry.get("source")
                   or "no metadata image — the page draws an identicon",
                   image_rank=entry.get("rank"))
    return rows



def status() -> dict:
    """The cache's health, without ever fetching anything."""
    with _LOCK:
        doc = _read_doc()
    images = doc.get("images") if isinstance(doc.get("images"), dict) else {}
    fetched = float(doc.get("fetched_at_unix") or 0)
    return {"standard": STANDARD, "source": str(doc.get("source") or "coingecko"),
            "store": STORE, "count": len(images),
            "fetched_at": doc.get("fetched_at"),
            "age_seconds": int(time.time() - fetched) if fetched else None,
            "ttl_hours": _ttl_hours(), "enabled": _enabled(),
            "dex_fallback": _dex_fallback(), "stale": _stale(doc),
            "logos": {**_mirror_doc(images),
                      "enabled": _enabled() and _mirror_enabled()},
            "last_error": _ATTEMPT.get("error")}


def refresh(*, mirror_logos: bool = True) -> dict:
    """Force a CoinGecko refresh now (the admin button / post-deploy hook).

    Whatever the refresh learned is mirrored straight away, so the pages keep
    drawing local files and a new ticker never waits for a second step.
    """
    report = catalog(refresh=True)
    logos = (mirror(limit=_mirror_limit()) if mirror_logos
             else {**_mirror_doc(report.get("images") or {}), "enabled": True,
                   "mirrored": 0, "skipped": 0, "failed": [], "bytes": 0})
    return {"refreshed_at": _iso(), "count": report["count"],
            "fetched_at": report["fetched_at"], "stale": report["stale"],
            "last_error": report["last_error"], "store": STORE, "logos": logos}


def remember(entries: dict, *, mirror_logos: bool = True) -> dict:
    """Store logos this node already knows: ``SYMBOL → {image, source, rank, name}``.

    The 120-token fleet and the launcher both mint one picture per asset; writing
    it here is what lets every other surface — the permanent token table, the
    launcher, the wallet page and the explorer — draw that same image without a
    second lookup, and keeps it for as long as the token exists. The
    picture is then mirrored onto this node, so a token minted today is served by
    ``/assets/tokens/`` exactly like the 120 already on chain. An entry without a
    URL is ignored.
    """
    stored = 0
    learned: list[str] = []
    with _LOCK:
        doc = _read_doc()
        images = doc.get("images") if isinstance(doc.get("images"), dict) else {}
        for symbol, entry in (entries or {}).items():
            ticker = str(symbol or "").strip().upper()
            row = entry if isinstance(entry, dict) else {}
            image = str(row.get("image") or "").strip()
            if not ticker or not image:
                continue
            fresh = _entry(image, str(row.get("source") or "market feed"),
                           rank=row.get("rank"), coin_id=row.get("coingecko_id"),
                           name=row.get("name"))
            previous = images.get(ticker)
            if isinstance(previous, dict) and str(previous.get("image") or "") == image:
                # same picture: keep its mirror pointer instead of copying again
                for key in ("logo_file", "logo_bytes", "logo_sha256", "logo_type",
                            "logo_at"):
                    if previous.get(key):
                        fresh[key] = previous[key]
            images[ticker] = fresh
            learned.append(ticker)
            stored += 1
        if stored:
            doc.update(images=images, standard=STANDARD, store=STORE,
                       source=str(doc.get("source") or "coingecko"),
                       mirror={**_mirror_doc(images),
                               "at": str((doc.get("mirror") or {}).get("at") or "")},
                       fetched_at=doc.get("fetched_at") or _iso(),
                       fetched_at_unix=int(doc.get("fetched_at_unix") or time.time()))
            _write_doc(doc)
    report = {"standard": STANDARD, "stored": stored, "count": len(images),
              "store": STORE}
    if stored and mirror_logos and _enabled() and _mirror_enabled():
        logos = _mirror_entries(learned, limit=_mirror_limit())
        report.update(mirrored=logos["mirrored"], failed=logos["failed"],
                      logos={key: logos[key] for key in ("count", "total",
                                                        "dir", "route")})
    return report

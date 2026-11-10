"""The 120-asset feed: three providers per list, one logo per asset, cached.

Fully offline — ``market_feed._fetch_json`` is replaced with a router over the
provider URLs, so the fallback order, the normalisation and the cache are all
exercised without a single network call.
"""
from __future__ import annotations

import pytest

from oneness_tor.services import market_feed

MARKETS = "https://api.coingecko.com/api/v3/coins/markets"


def _coingecko(count: int = 3, prefix: str = "MAJ") -> list[dict]:
    return [{"id": f"coin-{index}", "symbol": f"{prefix}{index}",
             "name": f"Coin {index}", "current_price": 1.25 * index,
             "market_cap": 1_000_000 * index, "market_cap_rank": index,
             "total_supply": 5_000 * index, "circulating_supply": 4_000 * index,
             "image": f"https://img.example/{prefix.lower()}{index}.png"}
            for index in range(1, count + 1)]


def _paprika(count: int = 3) -> list[dict]:
    return [{"id": f"pap-{index}", "name": f"Paprika {index}",
             "symbol": f"PAP{index}", "rank": index,
             "total_supply": 9_000 * index, "max_supply": 10_000 * index,
             "quotes": {"USD": {"price": 2.5 * index,
                                "market_cap": 2_000_000 * index}}}
            for index in range(1, count + 1)]


def _coinlore(count: int = 3) -> dict:
    return {"data": [{"id": str(index), "name": f"Lore {index}",
                      "symbol": f"LOR{index}", "rank": str(index),
                      "price_usd": "3.5", "market_cap_usd": "3000000",
                      "csupply": "100", "tsupply": "200"}
                     for index in range(1, count + 1)]}


@pytest.fixture(autouse=True)
def fresh_feed(sandbox, monkeypatch):
    """A sandboxed store, fetching switched on, and no inherited retry throttle."""
    monkeypatch.setenv("ONENESS_UNIVERSE", "1")
    monkeypatch.setattr(market_feed, "_ATTEMPT", {"at": 0.0, "errors": []})
    return sandbox


def _router(handlers):
    """``_fetch_json`` replacement: first handler whose key is in the URL wins."""
    calls: list[str] = []

    def fetch(url: str, timeout: float | None = None):
        calls.append(url)
        for key, answer in handlers.items():
            if key in url:
                value = answer() if callable(answer) else answer
                return value if isinstance(value, tuple) else (value, None)
        return None, "no route"

    fetch.calls = calls
    return fetch


def test_coingecko_is_primary_and_every_row_is_normalised(monkeypatch):
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "category=solana-meme-coins": _coingecko(1, "MEM"),
        "coingecko": _coingecko(3),
    }))
    doc = market_feed.universe(majors=3, memes=1)
    assert doc["count"] == 4
    assert [row["symbol"] for row in doc["rows"]] == ["MAJ1", "MAJ2", "MAJ3", "MEM1"]
    assert doc["majors"]["source"] == "coingecko"
    assert doc["memes"]["source"] == "coingecko"
    first = doc["rows"][0]
    assert first["supply"] == 5_000 and first["supply_basis"] == "total"
    assert first["price_usd"] == 1.25 and first["market_cap_usd"] == 1_000_000
    assert first["image"].endswith("maj1.png")
    assert first["image_source"] == "coingecko"
    assert first["source_id"] == "coin-1"


def test_the_first_fallback_answers_when_coingecko_fails(monkeypatch):
    fetch = _router({
        "coingecko": (None, "HTTP 429 from coingecko"),
        "coinpaprika": _paprika(3),
        "geckoterminal": {"data": [{
            "id": "solana_pool", "type": "pool",
            "attributes": {"base_token_price_usd": "0.5", "market_cap_usd": "900",
                           "name": "WIF / SOL"},
            "relationships": {"base_token": {"data": {"id": "solana_mint1"}}}}]},
        "latest/dex/tokens": {"pairs": [{
            "chainId": "solana", "priceUsd": "0.5", "marketCap": 900,
            "baseToken": {"address": "mint1", "symbol": "WIF", "name": "dogwifhat"},
            "info": {"imageUrl": "https://img.example/wif.png"}}]},
        "token-profiles": [],
    })
    monkeypatch.setattr(market_feed, "_fetch_json", fetch)
    doc = market_feed.universe(majors=3, memes=1)
    assert doc["majors"]["source"] == "coinpaprika"
    assert [row["symbol"] for row in doc["rows"]] == ["PAP1", "PAP2", "PAP3", "WIF"]
    assert doc["rows"][0]["supply"] == 9_000
    assert doc["rows"][0]["image"] == ""          # CoinPaprika publishes no logo
    assert doc["rows"][3]["image"] == "https://img.example/wif.png"
    attempted = [item["source"] for item in doc["majors"]["attempts"]]
    assert attempted[:2] == ["coingecko", "coinpaprika"]
    assert any("HTTP 429" in error for error in doc["errors"])


def test_the_second_fallback_answers_when_both_others_fail(monkeypatch):
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "coingecko": (None, "HTTP 500"),
        "coinpaprika": (None, "HTTP 502"),
        "coinlore": _coinlore(2),
        "geckoterminal": (None, "HTTP 503"),
        "token-profiles": [{"chainId": "solana", "tokenAddress": "mint9",
                            "icon": "https://img.example/meme9.png"}],
        "latest/dex/tokens": {"pairs": [{
            "chainId": "solana", "priceUsd": "0.01", "marketCap": 42_000,
            "baseToken": {"address": "mint9", "symbol": "MEME9", "name": "Meme Nine"},
            "info": {}}]},
    }))
    doc = market_feed.universe(majors=2, memes=1)
    assert doc["majors"]["source"] == "coinlore"
    assert doc["memes"]["source"] == "dexscreener"
    assert [row["symbol"] for row in doc["rows"]] == ["LOR1", "LOR2", "MEME9"]
    assert doc["rows"][0]["supply"] == 200          # tsupply, the total
    assert doc["rows"][2]["image"] == "https://img.example/meme9.png"
    assert doc["majors"]["attempts"][0]["ok"] is False


def test_bad_and_duplicate_tickers_are_dropped_not_minted(monkeypatch):
    rows = _coingecko(2)
    rows.append({**rows[0], "id": "dupe"})                  # same ticker twice
    rows.append({"symbol": "TOO-LONG-TICKER", "name": "Nope", "image": "x"})
    rows.append({"symbol": "币安人生", "name": "Nope", "image": "x"})
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "category=solana-meme-coins": [],
        "coingecko": rows,
        "geckoterminal": (None, "HTTP 500"),
        "token-profiles": [],
    }))
    doc = market_feed.universe(majors=2, memes=1)
    assert [row["symbol"] for row in doc["rows"]] == ["MAJ1", "MAJ2"]
    assert doc["duplicates"] == ["MAJ1"]
    assert doc["memes"]["returned"] == 0


def test_an_asset_without_published_supply_keeps_a_null_supply(monkeypatch):
    rows = _coingecko(1)
    rows[0]["total_supply"] = None
    rows[0]["circulating_supply"] = None
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "category=solana-meme-coins": [],
        "coingecko": rows,
        "geckoterminal": (None, "HTTP 500"),
        "token-profiles": [],
    }))
    row = market_feed.universe(majors=1, memes=0)["rows"][0]
    assert row["supply"] is None and row["supply_basis"] == ""


def test_a_missing_logo_is_filled_from_the_cached_public_metadata(monkeypatch):
    from oneness_tor.services import token_media

    monkeypatch.setattr(token_media, "lookup",
                        lambda symbol: {"image": f"https://cache/{symbol}.png",
                                        "source": "coingecko"})
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "coingecko": (None, "HTTP 429"),
        "coinpaprika": _paprika(1),
        "coinlore": (None, "HTTP 500"),
        "geckoterminal": (None, "HTTP 500"),
        "token-profiles": [],
    }))
    doc = market_feed.universe(majors=1, memes=0)
    row = doc["rows"][0]
    assert row["image"] == "https://cache/PAP1.png"
    assert row["image_source"].startswith("token-media cache")
    assert doc["images"] == {"with_image": 1, "filled_from_cache": 1,
                            "carried_from_previous": 0, "total": 1}


def test_a_fresh_cache_is_served_without_another_call(monkeypatch):
    fetch = _router({"category=solana-meme-coins": _coingecko(1, "MEM"),
                     "coingecko": _coingecko(2)})
    monkeypatch.setattr(market_feed, "_fetch_json", fetch)
    first = market_feed.universe(majors=2, memes=1)
    calls = len(fetch.calls)
    second = market_feed.universe(majors=2, memes=1)
    assert len(fetch.calls) == calls            # the cache answered
    assert second["rows"] == first["rows"]
    assert second["stale"] is False


def test_a_failed_refresh_keeps_the_previous_cache(monkeypatch):
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "category=solana-meme-coins": _coingecko(1, "MEM"),
        "coingecko": _coingecko(2)}))
    market_feed.universe(majors=2, memes=1)
    monkeypatch.setattr(market_feed, "_ATTEMPT", {"at": 0.0, "errors": []})
    monkeypatch.setattr(market_feed, "_fetch_json",
                        _router({"coingecko": (None, "HTTP 429"),
                                 "coinpaprika": (None, "HTTP 429"),
                                 "coinlore": (None, "HTTP 429"),
                                 "geckoterminal": (None, "HTTP 429"),
                                 "token-profiles": (None, "HTTP 429")}))
    doc = market_feed.universe(majors=2, memes=1, refresh=True)
    assert [row["symbol"] for row in doc["rows"]] == ["MAJ1", "MAJ2", "MEM1"]
    assert doc["count"] == 3                    # the table was not blanked
    assert len(doc["errors"]) >= 4
    assert doc["warning"] is None


def test_a_degraded_refresh_keeps_the_supply_and_logo_we_already_knew(monkeypatch):
    """A fallback provider without a supply must not erase the one we had."""
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "category=solana-meme-coins": _coingecko(1, "MEM"),
        "coingecko": _coingecko(2)}))
    first = market_feed.universe(majors=2, memes=1)
    assert [row["supply"] for row in first["rows"]] == [5_000, 10_000, 5_000]

    monkeypatch.setattr(market_feed, "_ATTEMPT", {"at": 0.0, "errors": []})
    monkeypatch.setattr(market_feed, "_fetch_json", _router({
        "coingecko": (None, "HTTP 429"),
        "coinpaprika": _paprika(2),
        "coinlore": (None, "HTTP 500"),
        "geckoterminal": (None, "HTTP 500"),
        "token-profiles": [{"chainId": "solana", "tokenAddress": "mint1", "icon": ""}],
        "latest/dex/tokens": {"pairs": [{
            "chainId": "solana", "priceUsd": "0.1", "marketCap": 900,
            "baseToken": {"address": "mint1", "symbol": "MEM1",
                          "name": "Meme One"}}]},
    }))
    doc = market_feed.universe(majors=2, memes=1, refresh=True)
    meme = next(row for row in doc["rows"] if row["symbol"] == "MEM1")
    assert meme["supply"] == 5_000
    assert meme["supply_source"] == "coingecko"
    assert meme["image"].endswith("mem1.png")
    assert doc["images"]["carried_from_previous"] == 2


def test_no_provider_and_no_cache_says_what_to_do(monkeypatch):
    monkeypatch.setattr(market_feed, "_fetch_json",
                        _router({"coingecko": (None, "HTTP 429"),
                                 "coinpaprika": (None, "HTTP 429"),
                                 "coinlore": (None, "HTTP 429"),
                                 "geckoterminal": (None, "HTTP 429"),
                                 "token-profiles": (None, "HTTP 429")}))
    doc = market_feed.universe(majors=2, memes=1)
    assert doc["rows"] == [] and doc["count"] == 0
    assert "universe/refresh" in doc["warning"]


def test_status_reads_the_cache_without_fetching(monkeypatch):
    fetch = _router({"category=solana-meme-coins": _coingecko(1, "MEM"),
                     "coingecko": _coingecko(2)})
    monkeypatch.setattr(market_feed, "_fetch_json", fetch)
    market_feed.universe(majors=2, memes=1)
    calls = len(fetch.calls)
    report = market_feed.status()
    assert len(fetch.calls) == calls
    assert report["majors"] == 2 and report["memes"] == 1
    assert report["with_image"] == 3 and report["stale"] is False

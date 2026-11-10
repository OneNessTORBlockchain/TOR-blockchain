"""Token logos: public-metadata resolution, the disk cache and the page contract.

Fully offline: ``token_media._fetch_json`` is monkeypatched with CoinGecko and
DexScreener shaped answers, so the mapping, the 24 h cache, the failure path and
the two HTTP endpoints are all exercised without a single network call.
"""
from __future__ import annotations

import json

import pytest

from oneness_tor.services import token_media

COINGECKO_ROWS = [
    {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
     "image": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png",
     "market_cap_rank": 1},
    {"id": "ethereum", "symbol": "eth", "name": "Ethereum",
     "image": "https://assets.coingecko.com/coins/images/279/large/ethereum.png",
     "market_cap_rank": 2},
    # a lower-cap coin reusing the BTC ticker must not steal the logo
    {"id": "fake-btc", "symbol": "btc", "name": "Fake Bitcoin",
     "image": "https://example.invalid/fake.png", "market_cap_rank": 900},
    {"id": "no-image", "symbol": "NONE", "name": "No Image", "image": "",
     "market_cap_rank": 5},
]


@pytest.fixture
def online(sandbox, monkeypatch):
    """Enable fetching and reset the module's backoff state for one test."""
    monkeypatch.setenv("ONENESS_TOKEN_IMAGES", "1")
    monkeypatch.setattr(token_media, "_may_attempt", lambda: True)
    monkeypatch.setattr(token_media, "_ATTEMPT", {"at": 0.0, "error": None})
    monkeypatch.setattr(token_media, "_MISSES", {})
    return monkeypatch


def test_catalog_maps_symbols_and_caches_them_on_disk(sandbox, online):
    seen = {}

    def fetch(url):
        seen["url"] = url
        return COINGECKO_ROWS, None

    online.setattr(token_media, "_fetch_json", fetch)
    doc = token_media.catalog()

    assert doc["count"] == 2                        # BTC + ETH (dupe + empty skipped)
    assert doc["images"]["BTC"]["image"].endswith("/bitcoin.png")
    assert doc["images"]["BTC"]["rank"] == 1
    assert doc["images"]["ETH"]["coingecko_id"] == "ethereum"
    assert "market_cap_desc" in seen["url"] and "per_page=250" in seen["url"]
    assert doc["stale"] is False and doc["last_error"] is None

    store = sandbox / "metadata" / "token-images.json"
    assert store.is_file()
    written = json.loads(store.read_text(encoding="utf-8"))
    assert written["images"]["BTC"]["source"] == "coingecko"


def test_a_failed_download_keeps_the_previous_cache(online):
    online.setattr(token_media, "_fetch_json",
                   lambda url: (COINGECKO_ROWS, None))
    token_media.catalog()
    online.setattr(token_media, "_fetch_json", lambda url: (None, "HTTP 429"))
    online.setattr(token_media, "_stale", lambda doc: True)

    doc = token_media.catalog()
    assert doc["count"] == 2 and doc["images"]["BTC"]["image"]
    assert doc["last_error"] == "HTTP 429"


def test_lookup_covers_the_native_coin_and_says_nothing_for_unknown_tickers(online):
    online.setattr(token_media, "_fetch_json", lambda url: (COINGECKO_ROWS, None))
    native = token_media.lookup("tor")
    assert native["image"].startswith("https://") and "IPFS" in native["source"]
    assert token_media.lookup("BTC")["source"] == "coingecko"
    assert token_media.lookup("NOPE") == {}


def test_dexscreener_is_the_fallback_for_tickers_coingecko_does_not_know(online):
    dex = {"pairs": [
        {"baseToken": {"name": "Ghost"}, "liquidity": {"usd": 10},
         "info": {"imageUrl": "https://example.invalid/small.png"}},
        {"baseToken": {"name": "Ghost"}, "liquidity": {"usd": 90000},
         "info": {"imageUrl": "https://example.invalid/big.png"}},
    ]}

    def fetch(url):
        if "dexscreener" in url:
            return dex, None
        return COINGECKO_ROWS, None

    online.setattr(token_media, "_fetch_json", fetch)
    entry = token_media.lookup("GHOST")
    assert entry["image"] == "https://example.invalid/big.png"   # best liquidity
    assert entry["source"] == "dexscreener"
    assert token_media.lookup("GHOST")["source"] == "dexscreener"  # now cached


def test_decorate_prefers_launcher_then_metadata_then_an_identicon(online):
    online.setattr(token_media, "_fetch_json", lambda url: (COINGECKO_ROWS, None))
    rows = [{"symbol": "TOR"}, {"symbol": "BTC"},
            {"symbol": "LAUNCH"}, {"symbol": "GHOST"}]
    token_media.decorate(rows, launcher_images={"LAUNCH": "ipfs://bafy-launcher"})

    assert rows[0]["image_source"].startswith("pinned IPFS")
    assert rows[1]["image_source"] == "coingecko" and rows[1]["image_rank"] == 1
    # the table gets a gateway URL for <img src>; the raw URI is kept too
    assert rows[2]["image"].startswith("https://ipfs.io/ipfs/bafy-launcher")
    assert rows[2]["image_uri"] == "ipfs://bafy-launcher"
    assert rows[2]["image_source"] == "launcher image"
    assert rows[3]["image"] == "" and "identicon" in rows[3]["image_source"]


def test_the_images_endpoints_answer_and_gate_the_refresh(online, monkeypatch):
    from oneness_tor.app import create_app
    from oneness_tor.services import auth

    online.setattr(token_media, "_fetch_json", lambda url: (COINGECKO_ROWS, None))
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(auth, "expected_hash", lambda: None)
    client = create_app().test_client()

    public = client.get("/api/tokens/images")
    assert public.status_code == 200
    assert public.get_json()["data"]["images"]["BTC"]["source"] == "coingecko"

    assert client.post("/api/tokens/images/refresh", json={}).status_code == 403
    refreshed = client.post("/api/tokens/images/refresh", json={},
                            headers={"x-admin-token": "test-admin-token"})
    assert refreshed.status_code == 200
    body = refreshed.get_json()["data"]
    assert body["refreshed"]["count"] == 2 and body["images"]["images"]["ETH"]


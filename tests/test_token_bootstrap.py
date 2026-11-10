"""The top-N bootstrap must be ranked, idempotent and honest about failures."""
from __future__ import annotations

import pytest

from core import pricing
from core.errors import ValidationError
from oneness_tor.services import token_bootstrap

IMAGES = {
    "ETH": {"image": "https://x/eth.png", "source": "coingecko", "rank": 2,
            "coingecko_id": "ethereum", "name": "Ethereum"},
    "BTC": {"image": "https://x/btc.png", "source": "coingecko", "rank": 1,
            "coingecko_id": "bitcoin", "name": "Bitcoin"},
    "USDT": {"image": "https://x/usdt.png", "source": "coingecko", "rank": 3,
             "coingecko_id": "tether", "name": "Tether"},
    "BAD TICKER": {"image": "https://x/bad.png", "source": "coingecko", "rank": 4,
                   "name": "Not a ticker"},
    "BTC2": {"image": "https://x/btc2.png", "source": "dexscreener", "rank": None,
             "name": "Bitcoin Two"},
}


def fake_catalog(**_: object) -> dict:
    return {"standard": "oneness-tor-token-images/1",
            "store": "data/metadata/token-images.json",
            "fetched_at": "2026-01-01T00:00:00+00:00", "stale": False,
            "count": len(IMAGES), "images": IMAGES}


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setattr(token_bootstrap.token_media, "catalog", fake_catalog)
    monkeypatch.setattr(token_bootstrap.meme, "chain_tokens",
                        lambda: {"BTC": {"symbol": "BTC"}})
    return monkeypatch


def test_plan_is_ranked_and_marks_what_already_exists(ledger):
    document = token_bootstrap.plan(3)
    assert [row["symbol"] for row in document["tokens"]] == ["BTC", "ETH", "USDT"]
    assert [row["exists"] for row in document["tokens"]] == [True, False, False]
    assert document["to_create"] == 2 and document["existing"] == 1
    assert document["fee"]["tor_each"] == pricing.price("meme_token_create")
    assert document["fee"]["tor_total"] == 2 * pricing.price("meme_token_create")
    assert document["source"]["store"].endswith("token-images.json")


def test_invalid_tickers_are_dropped_and_unranked_ones_sort_last(ledger):
    symbols = [row["symbol"] for row in token_bootstrap.plan(10)["tokens"]]
    assert "BAD TICKER" not in symbols
    assert symbols[-1] == "BTC2"


def test_dry_run_never_transacts(ledger):
    def explode(**_: object) -> dict:
        raise AssertionError("dry_run must not touch the chain")

    ledger.setattr(token_bootstrap.meme, "create_token", explode)
    report = token_bootstrap.run(limit=3, dry_run=True)
    assert report["dry_run"] is True
    assert report["created"] == [] and report["failed"] == []
    assert report["skipped"] == ["BTC"]


def test_run_mints_only_missing_tickers(ledger):
    calls: list[dict] = []
    ledger.setattr(token_bootstrap.meme, "create_token",
                   lambda **kwargs: (calls.append(kwargs) or
                                     {"txid": f"tx_{kwargs['symbol']}", "height": 7}))
    report = token_bootstrap.run(limit=3, supply=500, dry_run=False)
    assert [entry["symbol"] for entry in report["created"]] == ["ETH", "USDT"]
    assert report["skipped"] == ["BTC"]
    assert report["failed"] == []
    assert [call["symbol"] for call in calls] == ["ETH", "USDT"]
    assert all(call["supply"] == 500 for call in calls)


def test_a_chain_failure_is_reported_per_ticker(ledger):
    from core.errors import FeeError

    def create(**kwargs):
        if kwargs["symbol"] == "ETH":
            raise FeeError("founder holds 0 TOR", details={"required_micro": 1})
        return {"txid": f"tx_{kwargs['symbol']}", "height": 8}

    ledger.setattr(token_bootstrap.meme, "create_token", create)
    report = token_bootstrap.run(limit=3, dry_run=False)
    assert [entry["symbol"] for entry in report["created"]] == ["USDT"]
    assert report["failed"][0]["symbol"] == "ETH"
    assert report["failed"][0]["code"] == "INSUFFICIENT_FEE"
    assert report["failed"][0]["stage"] == "token"


def test_pools_are_seeded_only_when_requested(ledger):
    pools: list[dict] = []
    ledger.setattr(token_bootstrap.meme, "create_token",
                   lambda **kwargs: {"txid": "tx", "height": 9})
    ledger.setattr(token_bootstrap.meme, "create_pool",
                   lambda **kwargs: (pools.append(kwargs) or {"pool": {"pair": "ETH/TOR"}}))
    report = token_bootstrap.run(limit=3, supply=1000, dry_run=False, pools=True,
                                 pool_tor=12.5)
    assert len(pools) == 2 and pools[0]["tor_amount"] == 12.5
    assert report["created"][0]["pool"] == {"pair": "ETH/TOR"}


def test_an_empty_metadata_cache_says_what_to_do(ledger):
    ledger.setattr(token_bootstrap.token_media, "catalog",
                   lambda **_: {"images": {}, "stale": True,
                                "store": "data/metadata/token-images.json"})
    with pytest.raises(ValidationError, match="refresh"):
        token_bootstrap.plan(10)

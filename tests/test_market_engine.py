"""The price engine, the USDC books, the project registry and the AI gateway.

These tests never touch the chain or the network: they exercise the maths and
the shapes the six platforms depend on, so a regression surfaces here instead of
on a live dashboard.
"""
from __future__ import annotations

import pytest

from oneness_tor.services import ai_gateway, market_engine, pool_books, projects


def test_tor_anchor_is_three_sixty():
    assert market_engine.anchor_usd() == pytest.approx(3.60)


def test_drift_is_capped_and_explained():
    quiet = market_engine._drift([], [], 3.6)
    assert quiet["drift"] == 0.0

    roaring = [{"volume_24h_tor": 10_000_000.0, "liquidity_tor": 1_000.0}]
    hot = market_engine._drift(roaring, [], 3.6)
    assert hot["drift"] == pytest.approx(0.15)
    assert "clamped" in hot["rule"]

    crashing = [{"volume_24h_tor": 0.0, "liquidity_tor": 1_000.0}]
    assert market_engine._drift(crashing, [], 3.6)["drift"] == 0.0


def test_usdc_book_quote_is_constant_product(sandbox):
    book = pool_books.register(symbol="TEST", token_reserve=1_000_000,
                               usdc_reserve=10_000, seed="test")
    assert book["price_usd"] == pytest.approx(0.01)
    assert book["liquidity_usd"] == pytest.approx(20_000)

    quote = pool_books.quote("TEST", "buy", 100)
    # 100 USDC in, 0.30% fee, then the constant-product output
    net = 100 * (1 - 0.003)
    expected = 1_000_000 * net / (10_000 + net)
    assert quote["amount_out"] == pytest.approx(round(expected, 8), rel=1e-6)
    assert quote["fee_bps"] == 30

    sell = pool_books.quote("TEST", "sell", 1_000)
    net_tokens = 1_000 * (1 - 0.003)
    assert sell["amount_out"] == pytest.approx(round(10_000 * net_tokens / (1_000_000 + net_tokens), 8), rel=1e-6)


def test_usdc_book_rejects_impossible_amounts(sandbox):
    from core.errors import NotFound, ValidationError

    pool_books.register(symbol="SMALL", token_reserve=10, usdc_reserve=10)
    with pytest.raises(ValidationError):
        pool_books.quote("SMALL", "buy", 0)
    with pytest.raises(ValidationError):
        pool_books.quote("SMALL", "buy", -5)
    with pytest.raises(ValidationError):
        pool_books.quote("SMALL", "sideways", 1)
    with pytest.raises(ValidationError):
        pool_books.quote("SMALL", "buy", "not-a-number")
    with pytest.raises(NotFound):
        pool_books.get("NOPE")


def test_banner_rotation_always_starts_with_the_permanent_slot(sandbox):
    rotation = projects.banners()
    assert rotation["slots"][0]["id"] == "oneness-tor-permanent"
    assert rotation["slots"][0]["always"] is True
    assert rotation["price_tor"] == 50
    assert rotation["rotation_seconds"] >= 4


def test_every_project_is_listed_with_its_urls():
    assert len(projects.PROJECTS) == 6
    ids = {project["id"] for project in projects.PROJECTS}
    assert ids == {"tor-onion-swap", "tor-screener", "darkweb-crypto",
                   "tor-pump", "tor-charts", "solarix-engine"}
    for project in projects.PROJECTS:
        assert project["logo"].startswith("/assets/images/projects/")
        assert project["path"].startswith("/")
        assert project["fee_wallets"]


def test_gateway_only_accepts_free_models():
    assert ai_gateway._free({"id": "vendor/model:free"})
    assert ai_gateway._free({"id": "vendor/model", "pricing": {"prompt": "0", "completion": "0"}})
    assert not ai_gateway._free({"id": "vendor/model", "pricing": {"prompt": "0.001", "completion": "0.002"}})


def test_chat_requires_a_prompt():
    with pytest.raises(Exception):
        ai_gateway.chat(prompt="", messages=[])


def test_persona_carries_the_onion_address():
    text = ai_gateway.persona()
    assert "4thdw6cvoacqtxpyvxziebiif5qmiwginb7wwgktsalmevrabqubpxid.onion" in text
    assert "3.60" in text


def test_market_endpoints_are_in_the_catalog():
    from core import api_catalog

    names = set(api_catalog.names())
    for name in ("market.overview", "market.tokens", "market.chart", "market.pools",
                 "market.swap", "market.projects", "market.ads", "market.ai_chat",
                 "market.pump_launch"):
        assert name in names, name

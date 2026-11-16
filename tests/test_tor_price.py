"""The 30 s TOR/USD volatility feed (Capitain's order, 2026-11-15).

The rule the whole ecosystem reads:

* one tick every 30 s;
* each tick moves the price by a random $0.10-$0.50 — the volatility budget;
* the direction follows real activity (24 h swaps + TOR volume), so a busy
  chain rises and a quiet one falls;
* the quote never leaves $3.00-$100.00, and a published engine quote wins until
  the next tick.

Nothing here touches the chain or the network: these are the maths and the file
that every platform depends on.
"""
from __future__ import annotations

import random

import pytest

from core.errors import ValidationError
from oneness_tor.services import tor_price


def test_the_band_and_the_tick_are_the_published_ones():
    assert tor_price.tick_seconds() == pytest.approx(30.0)
    assert tor_price.step_min_usd() == pytest.approx(0.10)
    assert tor_price.step_max_usd() == pytest.approx(0.50)
    assert tor_price.floor_usd() == pytest.approx(3.00)
    assert tor_price.cap_usd() == pytest.approx(100.00)


def test_every_tick_moves_between_ten_and_fifty_cents():
    rng = random.Random(20261115)
    price = 50.0
    for _ in range(500):
        move = tor_price.next_price(price, 50.0, rng=rng)
        assert 0.10 - 1e-9 <= move["step_usd"] <= 0.50 + 1e-9
        assert move["price_usd"] == pytest.approx(price + (
            move["step_usd"] if move["direction"] == "up" else -move["step_usd"]))
        price = move["price_usd"]
        assert tor_price.floor_usd() <= price <= tor_price.cap_usd()


def test_the_floor_and_the_cap_are_absolute():
    rng = random.Random(1)
    price = tor_price.floor_usd()
    for _ in range(200):
        price = tor_price.next_price(price, tor_price.floor_usd(), rng=rng)["price_usd"]
        assert price >= tor_price.floor_usd()
    price = tor_price.cap_usd()
    for _ in range(200):
        price = tor_price.next_price(price, tor_price.cap_usd(), rng=rng)["price_usd"]
        assert price <= tor_price.cap_usd()


def test_activity_raises_the_target_from_three_to_one_hundred():
    quiet = tor_price.activity_score()
    assert quiet["score"] == 0.0
    assert tor_price.activity_target(quiet["score"]) == pytest.approx(3.00)

    busy = tor_price.activity_score(volume_tor=10_000, swaps=5_000)
    assert busy["score"] == 1.0
    assert tor_price.activity_target(busy["score"]) == pytest.approx(100.00)

    half = tor_price.activity_score(volume_tor=500, swaps=125)
    assert tor_price.activity_target(half["score"]) == pytest.approx(51.5)


def test_direction_follows_the_target():
    falling = tor_price.upward_bias(90.0, 3.0)
    rising = tor_price.upward_bias(3.0, 90.0)
    assert falling < 0.5 < rising
    assert 0.05 <= falling <= 0.95 and 0.05 <= rising <= 0.95


def test_quote_advances_once_per_tick_and_persists(sandbox):
    now = 1_800_000_000.0
    first = tor_price.quote(volume_tor=0, swaps=0, now=now)
    assert first["price_usd"] == pytest.approx(3.60)
    assert first["update_seconds"] == pytest.approx(30.0)
    assert first["price_usd"] >= tor_price.floor_usd()

    same = tor_price.quote(volume_tor=0, swaps=0, now=now + 10)
    assert same["price_usd"] == pytest.approx(first["price_usd"])

    later = tor_price.quote(volume_tor=0, swaps=0, now=now + 31)
    assert 0.10 - 1e-9 <= abs(later["price_usd"] - first["price_usd"]) <= 0.50 + 1e-9
    assert later["tick"] > first["tick"]

    assert (sandbox / "market" / "tor-price.json").exists()
    assert len(later["history"]) >= 2


def test_a_published_engine_quote_wins_until_the_next_tick(sandbox):
    now = 1_800_000_000.0
    tor_price.quote(now=now)
    published = tor_price.publish(42.5, actor="tor-price-engine", now=now + 1)
    assert published["price_usd"] == pytest.approx(42.5)
    assert published["source"] == "engine"
    assert published["published_by"] == "tor-price-engine"

    fresh = tor_price.quote(now=now + 10)
    assert fresh["price_usd"] == pytest.approx(42.5)
    assert fresh["source"] == "engine"

    # the engine is given two ticks of grace, then the node walks its own copy
    stale = tor_price.quote(now=now + 70)
    assert stale["source"] == "node-volatility"
    assert abs(stale["price_usd"] - 42.5) <= 2 * tor_price.step_max_usd() + 1e-9


def test_a_quote_outside_the_band_is_refused(sandbox):
    with pytest.raises(ValidationError):
        tor_price.publish(1.00)
    with pytest.raises(ValidationError):
        tor_price.publish(100.01)
    with pytest.raises(ValidationError):
        tor_price.publish("not-a-number")
    assert tor_price.publish(3.00)["price_usd"] == pytest.approx(3.00)


def test_peek_never_moves_the_price(sandbox):
    now = 1_800_000_000.0
    quoted = tor_price.quote(now=now)
    peeked = tor_price.peek(now=now + 3600)
    assert peeked["price_usd"] == pytest.approx(quoted["price_usd"])
    assert peeked["tick"] == quoted["tick"]

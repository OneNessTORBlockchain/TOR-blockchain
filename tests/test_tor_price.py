"""The 30 s TOR/USD volatility feed (Capitain's revised order, 2026-11-25).

The rule the whole ecosystem reads:

* one tick every 30 s;
* each tick moves the price by a random **$0.005-$0.05** — five cents is the
  ceiling, per tick, in either direction (the old walk allowed $0.10-$0.50);
* the whole walk lives inside a **$0.50 envelope** (±$0.25 around its anchor),
  so no amount of ticks can move TOR by more than half a dollar;
* the direction follows real activity (24 h swaps + TOR volume), and the
  activity target is itself kept inside the envelope;
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
    assert tor_price.step_min_usd() == pytest.approx(0.005)
    assert tor_price.step_max_usd() == pytest.approx(0.05)
    assert tor_price.band_usd() == pytest.approx(0.50)
    assert tor_price.band_half_usd() == pytest.approx(0.25)
    assert tor_price.floor_usd() == pytest.approx(3.00)
    assert tor_price.cap_usd() == pytest.approx(100.00)


def test_every_tick_moves_between_half_a_cent_and_five_cents():
    rng = random.Random(20261125)
    anchor = 3.60
    price = anchor
    for _ in range(1000):
        move = tor_price.next_price(price, tor_price.activity_target(0.5, anchor),
                                    rng=rng, anchor=anchor)
        assert move["step_usd"] <= 0.05 + 1e-9
        # a tick that lands on the envelope wall is pinned there, never jumped
        if not move["clamped"]:
            assert move["step_usd"] >= 0.005 - 1e-9
            assert move["price_usd"] == pytest.approx(price + (
                move["step_usd"] if move["direction"] == "up" else -move["step_usd"]))
        price = move["price_usd"]
        assert tor_price.band_low(anchor) - 1e-9 <= price <= tor_price.band_high(anchor) + 1e-9


def test_the_envelope_is_half_a_dollar_wide_and_absolute():
    anchor = 3.60
    assert tor_price.band_low(anchor) == pytest.approx(3.35)
    assert tor_price.band_high(anchor) == pytest.approx(3.85)
    rng = random.Random(7)
    # 2 000 ticks of a permanently rising bias still cannot leave the envelope
    price = anchor
    for _ in range(2000):
        price = tor_price.next_price(price, tor_price.band_high(anchor),
                                     rng=rng, anchor=anchor)["price_usd"]
        assert price <= tor_price.band_high(anchor) + 1e-9
    # ... and a permanently falling bias cannot leave the other side
    for _ in range(2000):
        price = tor_price.next_price(price, tor_price.band_low(anchor),
                                     rng=rng, anchor=anchor)["price_usd"]
        assert price >= tor_price.band_low(anchor) - 1e-9


def test_the_floor_and_the_cap_survive_an_anchor_outside_them():
    assert tor_price.band_high(99.99) <= tor_price.cap_usd()
    assert tor_price.band_low(0.01) >= tor_price.floor_usd()


def test_activity_target_stays_inside_the_envelope():
    anchor = 3.60
    quiet = tor_price.activity_score()
    assert quiet["score"] == 0.0
    assert tor_price.activity_target(quiet["score"], anchor) == pytest.approx(3.40)

    busy = tor_price.activity_score(volume_tor=10_000, swaps=5_000)
    assert busy["score"] == 1.0
    assert tor_price.activity_target(busy["score"], anchor) == pytest.approx(3.80)

    half = tor_price.activity_score(volume_tor=500, swaps=125)
    assert tor_price.activity_target(half["score"], anchor) == pytest.approx(3.60)


def test_direction_follows_the_target():
    falling = tor_price.upward_bias(3.80, 3.40)
    rising = tor_price.upward_bias(3.40, 3.80)
    assert falling < 0.5 < rising
    assert 0.05 <= falling <= 0.95 and 0.05 <= rising <= 0.95


def test_quote_advances_once_per_tick_and_persists(sandbox):
    now = 1_800_000_000.0
    first = tor_price.quote(volume_tor=0, swaps=0, now=now)
    assert first["price_usd"] == pytest.approx(3.60)
    assert first["update_seconds"] == pytest.approx(30.0)
    assert first["band_usd"] == pytest.approx(0.50)
    assert first["anchor_usd"] == pytest.approx(3.60)
    assert first["step_max_usd"] == pytest.approx(0.05)

    same = tor_price.quote(volume_tor=0, swaps=0, now=now + 10)
    assert same["price_usd"] == pytest.approx(first["price_usd"])

    later = tor_price.quote(volume_tor=0, swaps=0, now=now + 31)
    assert 0.005 - 1e-9 <= abs(later["price_usd"] - first["price_usd"]) <= 0.05 + 1e-9
    assert later["tick"] > first["tick"]

    assert (sandbox / "market" / "tor-price.json").exists()
    assert len(later["history"]) >= 2


def test_a_day_of_ticks_never_moves_the_price_by_more_than_50_cents(sandbox):
    now = 1_800_000_000.0
    first = tor_price.quote(volume_tor=10_000, swaps=5_000, now=now)
    last = tor_price.quote(volume_tor=10_000, swaps=5_000, now=now + 30 * 2880)
    assert abs(last["price_usd"] - first["price_usd"]) <= 0.50 + 1e-9
    assert tor_price.band_low(last["anchor_usd"]) - 1e-9 <= last["price_usd"]
    assert last["price_usd"] <= tor_price.band_high(last["anchor_usd"]) + 1e-9


def test_a_legacy_state_outside_the_envelope_is_re_anchored(sandbox):
    """The old walk could leave a state file at $42: it resumes there, in a band."""
    market = sandbox / "market"
    market.mkdir(parents=True, exist_ok=True)
    (market / "tor-price.json").write_text(
        '{"price_usd": 42.17, "anchor_usd": 3.6, "updated_ts": 1800000000,'
        ' "updated_at": "2027-01-15T08:00:00+00:00", "source": "node-volatility",'
        ' "tick": 9, "history": [[1800000000, 42.17]]}',
        encoding="utf-8",
    )
    now = 1_800_000_000.0 + 31
    quote = tor_price.quote(now=now)
    assert quote["anchor_usd"] == pytest.approx(42.17)
    assert tor_price.band_low(42.17) - 1e-9 <= quote["price_usd"] <= tor_price.band_high(42.17) + 1e-9
    assert abs(quote["price_usd"] - 42.17) <= 0.05 + 1e-9


def test_a_published_engine_quote_wins_until_the_next_tick(sandbox):
    now = 1_800_000_000.0
    tor_price.quote(now=now)
    published = tor_price.publish(3.72, actor="tor-price-engine", now=now + 1)
    assert published["price_usd"] == pytest.approx(3.72)
    assert published["source"] == "engine"
    assert published["published_by"] == "tor-price-engine"

    fresh = tor_price.quote(now=now + 10)
    assert fresh["price_usd"] == pytest.approx(3.72)
    assert fresh["source"] == "engine"

    # the engine is given two ticks of grace, then the node walks its own copy
    stale = tor_price.quote(now=now + 70)
    assert stale["source"] == "node-volatility"
    assert abs(stale["price_usd"] - 3.72) <= 2 * tor_price.step_max_usd() + 1e-9


def test_a_quote_outside_the_hard_band_is_refused(sandbox):
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


def test_status_publishes_the_envelope(sandbox):
    tor_price.quote(now=1_800_000_000.0)
    block = tor_price.status()
    assert block["standard"] == "oneness-tor-price-walk/2"
    assert block["band_usd"] == pytest.approx(0.50)
    assert block["band_high_usd"] - block["band_low_usd"] == pytest.approx(0.50)
    assert "5 cents" in block["rule"]


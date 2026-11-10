"""The admin panel's fee schedule: live prices, the LOCKED guard, reset, one table.

Everything here goes through ``core.pricing``, which is what the fee router
charges — so a saved price is asserted twice: once on the service and once on the
API that pages read.
"""
from __future__ import annotations

import json

import pytest

from core import pricing
from core.errors import ValidationError
from oneness_tor.services import payments, service_fees


@pytest.fixture(autouse=True)
def fresh_price_cache(sandbox, monkeypatch):
    """One store per test, and no ``config.yaml`` repricing unless a test sets it."""
    monkeypatch.delitem(pricing.settings.raw, "pricing", raising=False)
    monkeypatch.setattr(pricing, "_CACHE",
                        {"mtime": None, "data": {"overrides": {}, "history": []}})


def test_repricing_an_unlocked_service_is_live_everywhere(sandbox):
    assert pricing.price("meme_banner_7d") == 30.0
    applied = service_fees.update({"meme_banner_7d": 45.5}, actor="admin-session")

    assert applied["applied_codes"] == ["meme_banner_7d"]
    assert applied["applied"][0]["previous_tor"] == 30.0
    assert pricing.price("meme_banner_7d") == 45.5
    assert payments.quote("meme_banner_7d")["tor"] == 45.5
    assert pricing.micro("meme_banner_7d") == int(round(45.5 * pricing.MICRO_PER_TOR))

    store = json.loads(pricing.store_path().read_text(encoding="utf-8"))
    assert store["overrides"]["meme_banner_7d"]["tor"] == 45.5
    assert store["overrides"]["meme_banner_7d"]["set_by"] == "admin-session"
    assert store["history"][-1]["to"] == 45.5


def test_a_locked_row_needs_an_explicit_confirmation(sandbox):
    with pytest.raises(ValidationError) as blocked:
        service_fees.update({"contract_create": 6})
    assert blocked.value.code == "PRICE_LOCKED"
    assert pricing.price("contract_create") == 5.0

    service_fees.update({"contract_create": 6}, confirm_locked=True)
    assert pricing.price("contract_create") == 6.0
    row = next(item for item in pricing.admin_table()["rows"]
               if item["code"] == "contract_create")
    assert row["forced"] is True and row["source"] == "admin"
    assert row["default_tor"] == 5.0           # the published value stays visible


def test_reset_restores_the_published_table(sandbox):
    service_fees.update({"mint": 4, "meme_trending_day": 8}, confirm_locked=True)

    reset = service_fees.reset("mint")
    assert reset["removed"]["tor"] == 3.0
    assert pricing.price("mint") == 3.0
    assert pricing.price("meme_trending_day") == 8.0

    cleared = service_fees.reset(None)
    assert cleared["overrides"] == 0
    assert {row["code"]: row["tor"] for row in cleared["rows"]} == \
           {row["code"]: row["tor"] for row in pricing.defaults_table()}


def test_config_overrides_still_apply_under_the_admin_store(sandbox, monkeypatch):
    monkeypatch.setitem(pricing.settings.raw, "pricing",
                        {"overrides": {"api_key": 12}})
    assert pricing.price("api_key") == 12
    assert next(item for item in pricing.table()
                if item["code"] == "api_key")["source"] == "config"

    service_fees.update({"api_key": 20})
    assert pricing.price("api_key") == 20      # the panel wins over config.yaml
    service_fees.reset("api_key")
    assert pricing.price("api_key") == 12      # back to config, not to 25


def test_bad_prices_are_refused_with_a_usable_code(sandbox):
    with pytest.raises(ValidationError):
        pricing.set_price("meme_trending_day", 0)
    with pytest.raises(ValidationError):
        pricing.set_price("meme_trending_day", "free")
    with pytest.raises(ValidationError, match="unknown price code"):
        pricing.set_price("nope", 1)


def test_the_admin_api_sets_and_resets_prices(sandbox, monkeypatch):
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "fees-test-token")
    from oneness_tor.app import create_app

    http = create_app().test_client()
    auth = {"x-admin-token": "fees-test-token"}

    assert http.get("/api/admin/fees").status_code == 403
    table = http.get("/api/admin/fees", headers=auth).json["data"]
    assert table["count"] == len(pricing.PRICE_TABLE)
    assert all(row["tor"] == row["default_tor"] for row in table["rows"])

    saved = http.post("/api/admin/fees", headers=auth,
                      json={"prices": {"nft_mint": 4.5}}).json["data"]
    assert saved["applied_codes"] == ["nft_mint"]

    live = {row["code"]: row for row in
            http.get("/api/pricing").json["data"]["rows"]}
    assert live["nft_mint"]["tor"] == 4.5
    assert live["nft_mint"]["source"] == "admin"

    locked = http.post("/api/admin/fees", headers=auth,
                       json={"prices": {"mint": 9}})
    assert locked.status_code == 400
    assert locked.json["error"]["code"] == "PRICE_LOCKED"

    reset = http.delete("/api/admin/fees/nft_mint", headers=auth).json["data"]
    assert reset["removed"]["tor"] == 3.0
    assert http.delete("/api/admin/fees", headers=auth).json["data"]["overrides"] == 0

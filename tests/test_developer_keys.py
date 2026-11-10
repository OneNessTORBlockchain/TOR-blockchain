"""Developer API keys: paid in TOR, stored hashed, required by /api/v1.

The chain charge is stubbed (a unit test must not broadcast), but the key
store, the digest, expiry, revocation, rate limit and the gateway forwarding
are the production code paths.
"""
from __future__ import annotations

import hashlib
import time

import pytest

from oneness_tor.services import accounts, developer_keys, ledger, wallet_access

PASSWORD = "correct horse battery"


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(ledger.node, "account", lambda address: {
        "ok": True, "data": {"address": address, "balance": 900.0,
                             "balance_micro": 90_000_000_000, "nonce": 3,
                             "tokens": {}, "role": "holder"}})
    developer_keys._USAGE.clear()
    yield
    developer_keys._USAGE.clear()


@pytest.fixture
def charged(monkeypatch):
    calls: list[tuple] = []

    def fake_charge(wallet_name, code, *, quantity=1, note=""):
        calls.append((wallet_name, code, float(quantity), note))
        micro = int(round(25.0 * float(quantity) * 10 ** 8))
        return {"code": code, "label": "Developer API key — 30 days",
                "amount_micro": micro, "amount_tor": f"{25.0 * float(quantity):g}",
                "fee_receiver": "TOR1feeonion", "from": "TOR1payeronion",
                "txid": "tx_dev_1", "height": 12, "balance": 800 * 10 ** 8}

    monkeypatch.setattr(ledger, "charge", fake_charge)
    return calls


def _account(http):
    response = http.post("/api/auth/signup",
                         json={"username": "devuser", "password": PASSWORD})
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]
    return data, {wallet_access.HEADER: data["session"]["session"]}


def test_pricing_is_public_and_the_gateway_needs_a_key(sandbox, charged):
    from core import pricing as pricing_table

    http = client()
    pricing = http.get("/api/dev/keys/pricing").get_json()["data"]["pricing"]
    # the operator may reprice an API key in config.yaml (pricing.overrides);
    # the endpoint must echo whatever the price table says
    assert pricing["price"]["tor_per_30_days"] == pricing_table.price("api_key")
    assert pricing["header"] == "x-api-key"
    assert pricing["price"]["code"] == "api_key"

    denied = http.get("/api/v1/coin.get")
    assert denied.status_code == 403
    assert denied.get_json()["error"]["code"] == "FORBIDDEN"
    assert "x-api-key" in denied.get_json()["error"]["details"]["header"]

    unknown = http.get("/api/v1/coin.get", headers={"x-api-key": "otd_nope"})
    assert unknown.status_code == 403
    assert "not valid" in unknown.get_json()["error"]["message"]


def test_buying_a_key_charges_tor_and_stores_only_the_hash(sandbox, charged):
    http = client()
    data, headers = _account(http)
    bought = http.post("/api/dev/keys",
                       json={"label": "trading bot", "days": 30}, headers=headers)
    assert bought.status_code == 200, bought.get_data(as_text=True)
    key = bought.get_json()["data"]

    assert key["key"].startswith(developer_keys.KEY_PREFIX)
    assert key["shown_once"] is True and key["header"] == "x-api-key"
    assert key["charge"]["amount_tor"] == "25"
    assert charged and charged[0][0] == data["wallet"]["name"]
    assert charged[0][1] == "api_key"

    stored = developer_keys.KEYS.all()
    assert len(stored) == 1
    assert stored[0]["key_sha3"] == hashlib.sha3_256(
        key["key"].encode("utf-8")).hexdigest()
    assert key["key"] not in str(stored)          # the plaintext never lands on disk

    listing = http.get("/api/dev/keys", headers=headers).get_json()["data"]
    assert listing["count"] == 1
    assert listing["keys"][0]["hint"] == key["key"][:10] + "…" + key["key"][-4:]
    assert "key" not in listing["keys"][0]


def test_a_key_calls_the_real_endpoint_and_counts_usage(sandbox, charged):
    http = client()
    _, headers = _account(http)
    key = http.post("/api/dev/keys", json={}, headers=headers).get_json()["data"]["key"]

    called = http.get("/api/v1/coin.get", headers={"x-api-key": key})
    assert called.status_code == 200, called.get_data(as_text=True)
    envelope = called.get_json()
    assert envelope["ok"] is True
    assert envelope["data"]["symbol"] == "TOR"
    assert envelope["meta"]["developer_key"]["calls"] == 1
    assert envelope["meta"]["request"]["path"] == "/api/coin"

    second = http.get("/api/v1/chain.health", headers={"x-api-key": key})
    assert second.status_code == 200, second.get_data(as_text=True)
    stored = developer_keys.KEYS.all()[0]
    assert stored["calls"] == 2 and stored["last_path"] == "/api/health"



def test_unknown_endpoint_and_the_gateway_itself_are_refused(sandbox, charged):
    http = client()
    _, headers = _account(http)
    key = http.post("/api/dev/keys", json={}, headers=headers).get_json()["data"]["key"]

    unknown = http.get("/api/v1/does.not.exist", headers={"x-api-key": key})
    assert unknown.status_code == 404

    itself = http.get("/api/v1/dev.call", headers={"x-api-key": key})
    assert itself.status_code == 400


def test_revoked_and_expired_keys_stop_working(sandbox, charged):
    http = client()
    _, headers = _account(http)
    bought = http.post("/api/dev/keys", json={}, headers=headers).get_json()["data"]
    key, key_id = bought["key"], bought["key_id"]

    revoked = http.delete(f"/api/dev/keys/{key_id}", headers=headers)
    assert revoked.status_code == 200, revoked.get_data(as_text=True)
    assert revoked.get_json()["data"]["revoked"] is True
    assert http.get("/api/v1/coin.get", headers={"x-api-key": key}).status_code == 403

    # a second key that is expired is refused with an explicit message
    fresh = http.post("/api/dev/keys", json={}, headers=headers).get_json()["data"]
    developer_keys.KEYS.update(fresh["key_id"], expires_unix=int(time.time()) - 60,
                               expires_at="2020-01-01T00:00:00+00:00")
    expired = http.get("/api/v1/coin.get", headers={"x-api-key": fresh["key"]})
    assert expired.status_code == 403
    assert "expired" in expired.get_json()["error"]["message"]


def test_rate_limit_answers_429(sandbox, charged, monkeypatch):
    http = client()
    _, headers = _account(http)
    key = http.post("/api/dev/keys", json={}, headers=headers).get_json()["data"]["key"]
    monkeypatch.setattr(developer_keys, "limits",
                        lambda: {"per_minute": 2, "per_day": 100})

    assert http.get("/api/v1/coin.get", headers={"x-api-key": key}).status_code == 200
    assert http.get("/api/v1/coin.get", headers={"x-api-key": key}).status_code == 200
    limited = http.get("/api/v1/coin.get", headers={"x-api-key": key})
    assert limited.status_code == 429
    assert limited.get_json()["error"]["code"] == "RATE_LIMITED"
    assert limited.get_json()["error"]["details"]["limits"]["per_minute"] == 2


def test_buying_requires_a_wallet_session(sandbox, charged):
    http = client()
    assert http.post("/api/dev/keys", json={}).status_code == 403
    data = http.post("/api/auth/signup",
                     json={"username": "devuser", "password": PASSWORD}).get_json()["data"]
    headers = {wallet_access.HEADER: data["session"]["session"]}
    # a sibling wallet of the same account may pay instead of the session wallet
    second = http.post("/api/auth/wallets", json={"password": PASSWORD},
                       headers=headers).get_json()["data"]["wallet"]["name"]
    paid = http.post("/api/dev/keys", json={"wallet": second}, headers=headers)
    assert paid.status_code == 200, paid.get_data(as_text=True)
    assert charged[-1][0] == second


def test_api_key_enforcement_switch_can_gate_the_whole_api(sandbox, charged, monkeypatch):
    """With api.require_key on, a bare /api call needs a key (or a session)."""
    from core.config import settings

    http = client()
    _, headers = _account(http)
    key = http.post("/api/dev/keys", json={}, headers=headers).get_json()["data"]["key"]
    monkeypatch.setitem(settings.raw, "api",
                        {**settings.raw.get("api", {}), "require_key": True})

    assert http.get("/api/contracts/templates").status_code == 403
    assert http.get("/api/contracts/templates",
                    headers={"x-api-key": key}).status_code == 200
    assert http.get("/api/auth/me", headers=headers).status_code == 200
    # the keyed gateway keeps working: its internal request carries the same key
    assert http.get("/api/v1/contracts.templates",
                    headers={"x-api-key": key}).status_code == 200

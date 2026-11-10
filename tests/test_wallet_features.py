"""Wallet surface: receive QR, 30-day history with hex, and no fiat rate.

Everything here runs against the real Flask app and the real service layer; only
the Rust node's HTTP answers are stubbed, because a unit test must not need a
running ledger. The shapes asserted are exactly what ``/api/wallets/<name>/
receive`` and ``/history`` return.

Prices on this node are TOR-denominated by design: there is no ``/api/usd-rate``
and no ``balance_usd`` field anywhere (USD will come from on-chain oracles later).
"""
from __future__ import annotations

import time

import base58
import pytest

from oneness_tor.services import ledger, token_registry, wallet_access



def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def _headers(name: str, address: str) -> dict:
    session = wallet_access.open_session(name, address)
    return {wallet_access.HEADER: session["session"]}


@pytest.fixture
def founder(make_wallet):
    return make_wallet("founder")


# ------------------------------------------------------------------- receive
def test_receive_exposes_the_public_key_and_a_real_qr(sandbox, founder):
    http = client()
    assert http.get("/api/wallets/founder/receive").status_code == 403

    response = http.get("/api/wallets/founder/receive",
                        headers=_headers("founder", founder["address"]))
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]

    assert data["address"] == founder["address"]
    assert data["pubkey_base58"] == founder["pubkey_base58"]
    assert data["pubkey_hex"] == base58.b58decode(founder["pubkey_base58"]).hex()
    assert data["chain_id"] == "072316"

    address_qr, pubkey_qr = data["qr"]["address"], data["qr"]["pubkey"]
    assert address_qr["text"] == founder["address"]
    assert address_qr["version"] >= 1 and address_qr["error_correction"] == "M"
    assert address_qr["svg"].startswith("data:image/svg+xml")
    assert pubkey_qr["text"] == founder["pubkey_base58"]
    assert pubkey_qr["version"] >= 1
    assert pubkey_qr["svg"].startswith("data:image/svg+xml")


def test_receive_never_returns_private_key_material(sandbox, founder):
    http = client()
    body = http.get("/api/wallets/founder/receive",
                    headers=_headers("founder", founder["address"])).get_data(as_text=True)
    assert founder["secret_key_base58"] not in body
    assert "mnemonic" not in body


# ------------------------------------------------------------------- history
def _record(**over) -> dict:
    now = int(time.time())
    base = {"txid": "ab" * 32, "kind": "transfer", "from": "TOR1someoneelseonion",
            "to": "TOR1meonion", "amount": 2 * 10 ** 8, "token": None, "fee": 0,
            "note": "note", "signature": base58.b58encode(b"\x11" * 64).decode(),
            "seq": 1, "timestamp": now - 3600, "height": 7,
            "block_hash": "cd" * 32, "visibility": "public"}
    base.update(over)
    return base


def _stub_records(monkeypatch, records: list[dict]) -> None:
    monkeypatch.setattr(ledger.node, "public_records",
                        lambda limit=500: {"ok": True, "data": {
                            "records": records[:limit]}})


def test_history_returns_every_record_with_hex_per_row(sandbox, founder, monkeypatch):
    address = founder["address"]
    other = "TOR1someoneelseonion"
    now = int(time.time())
    signature = base58.b58encode(b"\x22" * 64).decode()
    records = [
        _record(txid="aa" * 32, to=address),
        _record(txid="bb" * 32, **{"from": address, "to": other, "token": "MEME",
                                   "amount": 75, "seq": 2, "timestamp": now - 7200}),
        _record(txid="cc" * 32, **{"from": address, "to": "TOR1feereceiver",
                                   "amount": 10_000_000,
                                   "note": "node/platform transfer fee",
                                   "seq": 3, "timestamp": now - 900,
                                   "signature": signature}),
        _record(txid="dd" * 32, **{"from": other, "to": "TOR1thirdparty"}),
        # nothing expires any more: a year-old record is still this wallet's
        _record(txid="ee" * 32, to=address, timestamp=now - 400 * 86_400),
    ]
    _stub_records(monkeypatch, records)

    http = client()
    assert http.get("/api/wallets/founder/history").status_code == 403

    response = http.get("/api/wallets/founder/history?limit=500",
                        headers=_headers("founder", address))
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]

    assert data["address"] == address
    assert "days" not in data and "window_days" not in data
    assert data["scanned"] == 5 and data["count"] == 4
    assert [row["txid"] for row in data["records"]] == [
        "cc" * 32, "aa" * 32, "bb" * 32, "ee" * 32]

    newest = data["records"][0]
    assert newest["direction"] == "out"
    assert newest["signature_hex"] == (b"\x22" * 64).hex()
    assert newest["txid_hex"] == "cc" * 32 and newest["block_hash_hex"] == "cd" * 32
    assert newest["fee_tor"] == "0" and newest["amount_tor"] == "0.1"

    incoming = data["records"][1]
    assert incoming["direction"] == "in" and incoming["amount_tor"] == "2"

    token_row = data["records"][2]
    assert token_row["token"] == "MEME" and token_row["unit"] == "token"
    assert token_row["amount"] == 75 and token_row["amount_tor"] is None

    totals = data["totals"]
    # two incoming transfers of 2 TOR each (the second one is a year old)
    assert totals["in_tor"] == 4.0 and totals["out_tor"] == 0.1
    assert totals["tokens"]["MEME"] == {"in": 0.0, "out": 75.0}


def test_history_scan_limit_is_reported_not_silently_applied(sandbox, founder,
                                                              monkeypatch):
    """There is no date ceiling any more; only the scan limit can truncate."""
    _stub_records(monkeypatch, [_record(txid="aa" * 32, to=founder["address"])])
    http = client()
    body = http.get("/api/wallets/founder/history?limit=1",
                    headers=_headers("founder", founder["address"])).get_json()["data"]
    assert body["limit"] == 1 and body["truncated"] is True
    assert "whole history" in body["note"]


# ------------------------------------------------------------------- no fiat
def test_the_usd_reference_rate_is_gone(sandbox, founder, monkeypatch):
    """Every price is quoted in TOR; the old operator rate must not come back."""
    # patch the class, not the singleton: an instance-level patch would be
    # restored as a bound-method attribute and shadow later class-level patches
    monkeypatch.setattr(type(ledger.node), "account",
                        lambda self, address: {"ok": True, "data": {
                            "address": address, "balance": 12.5,
                            "balance_micro": 1_250_000_000, "nonce": 0,
                            "tokens": {"MEME": 5}, "role": "founder"}})


    http = client()
    assert http.get("/api/usd-rate").status_code == 404
    assert http.post("/api/usd-rate", json={"usd_per_tor": 0.42}).status_code == 404

    wallet = ledger.public_wallet("founder", with_balance=True)
    assert "balance_usd" not in wallet
    assert ledger.balance_of("founder").get("balance_usd") is None


def test_token_table_has_no_usd_block_and_prices_in_tor(sandbox, monkeypatch):
    from oneness_tor.services import meme

    monkeypatch.setattr(token_registry.node, "public_records",
                        lambda limit=500: {"ok": True, "data": {"records": []}})
    monkeypatch.setattr(token_registry.node, "info", lambda: {"ok": True, "data": {
        "total_supply_micro": 50_000_000 * 10 ** 8, "founder": "TOR1founder"}})
    monkeypatch.setattr(meme, "chain_tokens", lambda: {})
    monkeypatch.setattr(meme, "chain_pools", lambda: {})
    monkeypatch.setattr(meme, "public_images", lambda: {})

    snap = token_registry.snapshot()
    assert "usd" not in snap
    assert "fiat" in snap["note"]
    native = snap["tokens"][0]
    assert native["symbol"] == "TOR" and native["price_tor"] == 1.0
    assert native["image"].startswith("https://") and "IPFS" in native["image_source"]
    assert {"image", "image_source", "image_rank"} <= set(native)
    assert snap["images"]["standard"].startswith("oneness-tor-token-images/")




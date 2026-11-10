"""Balances, tokens and totals are permanent public data — at every layer.

These tests are offline: the Rust node is stubbed with the exact document it
serves on ``GET /api/public/ledger``, so the Python read model (and the Flask
routes built on it) are checked without a binary or a network.
"""
from __future__ import annotations

import pytest

from core.errors import ChainUnavailable, NotFound
from oneness_tor.services import public_ledger

NOW = 1_793_086_000
FOUNDER = "TOR12xd7zywc3iuwcls3fqzsozkgo5ahv3gnonion"
HOLDER = "TOR1psa5lxgc5w3vp5msn46ozjuibnbnyhkmonion"
POOL = "TOR1pool0000000000000000000000000000onion"

DOC = {
    "standard": public_ledger.STANDARD,
    "chain_id": "072316",
    "native_coin_id": "TOR111111111111111111111111111111onion",
    "height": 30, "head_hash": "aa" * 32, "genesis_hash": "bb" * 32,
    "micro_per_tor": 100_000_000,
    "total_supply_micro": 50_000_000 * 10 ** 8,
    "total_supply": 50_000_000.0,
    "founder": FOUNDER, "fee_receiver": FOUNDER,
    "counts": {"accounts": 3, "tokens": 1, "pools": 1, "records": 9},
    "totals": {"accounts": 3, "balance_micro": 50_000_000 * 10 ** 8,
               "balance": 50_000_000.0, "tokens": 1, "pools": 1, "holders": 1,
               "token_supply": {"LIVE1": 250_000}},
    "accounts_total": 3, "limit": 500, "offset": 0,
    "accounts": [
        {"address": FOUNDER, "balance_micro": 49_999_840 * 10 ** 8,
         "balance": 49_999_840.0, "token_balances": {"LIVE1": 153_735},
         "role": "founder", "created_at": NOW},
        {"address": HOLDER, "balance_micro": 100 * 10 ** 8,
         "balance": 100.0, "token_balances": {"LIVE1": 100},
         "role": "holder", "created_at": NOW},
        {"address": POOL, "balance_micro": 60 * 10 ** 8, "balance": 60.0,
         "token_balances": {"LIVE1": 96_165}, "role": "holder",
         "created_at": NOW},
    ],
    "tokens": {"LIVE1": {
        "symbol": "LIVE1", "name": "Live Proof LIVE1", "supply": 250_000,
        "decimals": 0, "owner": FOUNDER, "created_at": NOW,
        "in_pools": 96_165, "held": 250_000, "holders": 3,
        "circulating": 153_835}},
    "pools": {"LIVE1": {"token": "LIVE1", "token_reserve": 96_165,
                        "tor_reserve": 51_994_000_000, "fee_bps": 30,
                        "created_at": NOW}},
}


def _stub(monkeypatch, doc=None) -> dict:
    """Serve ``doc`` from the node, honouring the ledger's own paging."""
    body = doc or DOC
    public_ledger.reset()

    def fake(limit: int = 500, offset: int = 0) -> dict:
        page = body["accounts"][offset:offset + limit]
        return {"ok": True, "error": None, "meta": {}, "data": {
            **body, "accounts": page, "limit": limit, "offset": offset,
            "accounts_total": len(body["accounts"])}}

    monkeypatch.setattr(public_ledger.node, "public_ledger", fake)
    return body


def test_snapshot_keeps_every_total_and_pages_the_accounts(monkeypatch):
    _stub(monkeypatch)
    doc = public_ledger.snapshot()
    assert doc["standard"] == public_ledger.STANDARD
    assert doc["totals"]["accounts"] == 3
    assert len(doc["accounts"]) == 3

    one = public_ledger.snapshot(1, 1)
    assert [row["address"] for row in one["accounts"]] == [HOLDER]
    assert one["accounts_total"] == 3


def test_totals_report_supply_holders_and_pools(monkeypatch):
    _stub(monkeypatch)
    totals = public_ledger.totals()
    assert totals["total_supply_tor"] == 50_000_000.0
    assert totals["tokens"] == 1 and totals["pools"] == 1
    assert totals["accounts"] == 3
    assert totals["permanent"] is True


def test_token_detail_carries_circulating_pooled_and_holders(monkeypatch):
    _stub(monkeypatch)
    row = public_ledger.token("live1")
    assert row["symbol"] == "LIVE1"
    assert row["supply"] == 250_000
    assert row["in_pools"] == 96_165
    assert row["circulating"] == 153_835
    assert row["holders"] == 3
    assert row["pool"]["price_tor"] == pytest.approx(51_994_000_000 / 1e8 / 96_165)


def test_holders_are_every_address_largest_first(monkeypatch):
    _stub(monkeypatch)
    report = public_ledger.holders("LIVE1", limit=2)
    assert report["holders_total"] == 3 and report["count"] == 2
    assert [row["amount"] for row in report["holders"]] == [153_735, 96_165]
    assert report["holders"][0]["share_percent"] == pytest.approx(
        100 * 153_735 / 250_000, rel=1e-6)
    native = public_ledger.holders("TOR")
    assert native["native"] is True and native["holders_total"] == 3
    assert native["total_amount"] == 50_000_000 * 10 ** 8


def test_balances_sheet_is_public_and_formatted(monkeypatch):
    _stub(monkeypatch)
    sheet = public_ledger.balances(2, 0)
    assert sheet["count"] == 2 and sheet["accounts_total"] == 3
    row = sheet["accounts"][0]
    assert row["address"] == FOUNDER
    assert row["balance_tor"] == "49999840"
    assert row["token_balances"] == {"LIVE1": 153_735}
    assert row["token_count"] == 1



def test_one_address_balance_is_a_plain_read(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(public_ledger.node, "account", lambda address: {
        "ok": True, "error": None, "meta": {}, "data": {
            "address": address, "pubkey_base58": "5abc", "balance_micro": 12_345,
            "balance": 0.00012345, "tokens": {"LIVE1": 7}, "nonce": 2,
            "role": "holder", "created_at": NOW}})
    row = public_ledger.balance(HOLDER)
    assert row["balance_micro"] == 12_345
    assert row["balance_tor"] == "0.00012345"
    assert row["token_balances"] == {"LIVE1": 7}
    assert row["public"] is True and row["permanent"] is True


def test_unknown_token_and_unknown_address_are_not_invented(monkeypatch):
    _stub(monkeypatch)
    with pytest.raises(NotFound):
        public_ledger.token("NOPE")
    monkeypatch.setattr(public_ledger.node, "account", lambda address: {
        "ok": False, "error": {"code": "NOT_FOUND"}, "data": None, "meta": {}})
    with pytest.raises(NotFound):
        public_ledger.balance(HOLDER)


def test_node_down_still_publishes_the_public_constants(monkeypatch):
    public_ledger.reset()

    def down(*args, **kwargs):
        raise ChainUnavailable("the TOR node is down")

    monkeypatch.setattr(public_ledger.node, "public_ledger", down)
    monkeypatch.setattr(public_ledger.node, "info", lambda: {
        "ok": True, "data": {"total_supply_micro": 1_000, "founder": FOUNDER,
                             "chain_id": "072316", "height": 4}})
    monkeypatch.setattr(public_ledger.node, "tokens", lambda: {
        "ok": True, "data": {"LIVE1": {"symbol": "LIVE1", "supply": 100,
                                       "decimals": 0, "owner": FOUNDER}}})
    monkeypatch.setattr(public_ledger.node, "pools", lambda: {
        "ok": True, "data": {"LIVE1": {"token_reserve": 40,
                                       "tor_reserve": 10 ** 8}}})
    doc = public_ledger.snapshot()
    assert doc["node_reachable"] is True
    assert doc["tokens"]["LIVE1"]["in_pools"] == 40
    assert doc["tokens"]["LIVE1"]["circulating"] == 60
    assert doc["totals"]["tokens"] == 1
    assert doc["warning"]

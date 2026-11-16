"""The wallet payment for the $11.99/month membership, and the contract behind it.

Only the Rust node's HTTP answers are stubbed (the suite must not need a running
ledger): the USD->TOR arithmetic, the Ed25519 signature, the invoice, the
receipt, the account row and the contract state are production code.
"""
from __future__ import annotations

import time

import pytest

from oneness_tor.services import (contracts, ledger, membership, node_sync,
                                  wallet_access)

#: A real TOR1...onion address shape (the node's validator is strict about it).
TREASURY = "TOR1" + "t" * 30 + "onion"


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def _headers(name: str, address: str) -> dict:
    session = wallet_access.open_session(name, address)
    return {wallet_access.HEADER: session["session"]}


class FakeNode:
    """A stand-in for the ``core.node_client.node`` singleton.

    Owning the whole object (not patching one method) is the only order
    independent way to answer the node — see ``tests/test_privacy_protocol.py``.
    """

    def __init__(self) -> None:
        self.submitted: list[dict] = []

    def submit_tx(self, signed: dict) -> dict:
        self.submitted.append(signed)
        return {"ok": True, "data": {
            "accepted": 1, "height": 7, "head_hash": "ff" * 32,
            "records": [{"txid": f"{len(self.submitted):02x}" * 32,
                         "kind": signed.get("kind"), "from": signed["from"],
                         "to": signed["to"], "amount": signed["amount"],
                         "note": signed.get("note"),
                         "external_ref": signed.get("external_ref", "")}]}}

    def info(self) -> dict:
        return {"ok": True, "data": {"founder": TREASURY, "fee_receiver": TREASURY,
                                     "height": 7}}

    def fee_receiver(self) -> str:
        return TREASURY

    def account(self, address: str) -> dict:
        return {"ok": True, "data": {"address": address, "balance": 500.0,
                                     "balance_micro": 50_000_000_000, "nonce": 1,
                                     "tokens": {}, "role": "holder"}}

    def public_records(self, limit: int = 50) -> dict:
        return {"ok": True, "data": {"records": self.submitted[:limit]}}


@pytest.fixture
def node_stub(monkeypatch, sandbox):
    fake = FakeNode()
    monkeypatch.setattr(ledger, "node", fake)
    monkeypatch.setattr(membership, "node", fake)
    monkeypatch.setattr(node_sync, "is_replica", lambda: False)
    for name in ("ONENESS_MEMBERSHIP_CONTRACT", "ONENESS_MEMBERSHIP_USD",
                 "ONENESS_MEMBERSHIP_DAYS", "ONENESS_MEMBERSHIP_TREASURY",
                 "ONENESS_MEMBERSHIP_PLAN", "ONENESS_MEMBERSHIP_LABEL"):
        monkeypatch.delenv(name, raising=False)
    return fake


@pytest.fixture
def fixed_rate(monkeypatch):
    """Pin TOR at the published anchor: $3.60 = 3 600 000 micro-USD."""
    monkeypatch.setattr(membership, "tor_usd", lambda: 3.60)
    return 3.60


# ------------------------------------------------------------------ the quote
def test_the_plan_is_1199_usd_for_30_days(node_stub):
    plan = membership.plan()

    assert plan["usd"] == 11.99 and plan["days"] == 30
    assert plan["currency"] == "USD" and plan["settled_in"] == "TOR"
    # no treasury configured on this node -> the node's own fee receiver
    assert plan["treasury"] == TREASURY


def test_the_rate_comes_from_the_engine_and_falls_back_to_the_anchor(
        node_stub, monkeypatch):
    from oneness_tor.services import market_engine

    monkeypatch.setattr(market_engine, "snapshot",
                        lambda *a, **k: {"tor": {"price_usd": 4.5}})
    assert membership.tor_usd() == 4.5

    monkeypatch.setattr(market_engine, "snapshot", lambda *a, **k: {})
    monkeypatch.setattr(market_engine, "anchor_usd", lambda: 3.60)
    assert membership.tor_usd() == 3.60


def test_1199_usd_becomes_an_exact_micro_amount(node_stub, fixed_rate):
    quoted = membership.quote(1)

    assert quoted["usd"] == 11.99 and quoted["usd_cents"] == 1199
    assert quoted["days"] == 30 and quoted["tor_usd"] == 3.60
    # 11.99 / 3.60 * 10**8, rounded to the nearest microTOR
    assert quoted["micro"] == 333_055_556
    assert quoted["amount_tor"] == "3.33055556"
    assert membership.micro_for(11.99, 3.60) == quoted["micro"]
    # the amount is linear in the rate and in the months bought
    assert membership.micro_for(11.99, 7.20) == 166_527_778
    assert membership.quote(2)["micro"] == 666_111_111
    assert membership.quote(2)["days"] == 60


def test_a_zero_rate_or_price_is_refused_rather_than_invented(node_stub):
    with pytest.raises(Exception):
        membership.micro_for(11.99, 0)
    with pytest.raises(Exception):
        membership.quote(0)


# ------------------------------------------------------------- the payment
def test_paying_an_invoice_settles_on_chain_and_records_the_period(
        sandbox, make_wallet, node_stub, fixed_rate):
    wallet = make_wallet("founder")
    opened = membership.create_invoice(wallet="founder",
                                       address=wallet["address"])["invoice"]
    assert opened["id"] == "sub_0001" and opened["status"] == "open"
    assert opened["treasury"] == TREASURY and opened["micro"] == 333_055_556

    paid = membership.pay(opened["id"], "founder")

    assert paid["invoice"]["status"] == "paid"
    assert paid["txid"] and paid["height"] == 7
    transfer = node_stub.submitted[0]
    assert transfer["to"] == TREASURY
    assert transfer["amount"] == opened["micro"]          # the frozen amount
    assert transfer["external_ref"] == ""                  # a public payment
    assert paid["receipt"]["txid"] == paid["txid"]
    assert paid["receipt"]["amount_tor"] == "3.33055556"

    member = membership.status(wallet["address"])
    assert member["member"] is True and member["active"] is True
    assert member["payments"] == 1
    assert member["paid_micro"] == opened["micro"]
    assert member["expires_at"] >= int(time.time()) + 30 * 86_400 - 5
    assert member["days_left"] > 29

    with pytest.raises(Exception):
        membership.pay(opened["id"], "founder")           # never paid twice


def test_an_anonymous_membership_payment_hides_the_payer_but_not_the_receipt(
        sandbox, make_wallet, node_stub, fixed_rate):
    wallet = make_wallet("founder")
    opened = membership.create_invoice(wallet="founder",
                                       address=wallet["address"],
                                       privacy="anonymous")["invoice"]

    paid = membership.pay(opened["id"], "founder", privacy="anonymous")

    assert paid["settlement"]["privacy"] == "anonymous"
    assert paid["settlement"]["on_chain_from"] == "Tor-Anonymous-onion"
    assert paid["invoice"]["privacy"] == "anonymous"
    assert node_stub.submitted[0]["external_ref"] == "oneness-privacy:1|mode=anonymous"
    # the buyer's own receipt still names the wallet that paid
    assert paid["receipt"]["address"] == wallet["address"]
    assert membership.status(wallet["address"])["last_txid"] == paid["txid"]


# ------------------------------------------------------------ the contract
def _deploy_membership_contract(make_wallet, monkeypatch) -> dict:
    """Register the membership program in the registry (no chain charge here)."""
    wallet = make_wallet("founder")
    template = contracts.TEMPLATES["membership"]
    row = contracts.CONTRACTS.add({
        "kind": "contract", "name": "OneNess TOR membership",
        "template": "membership", "owner": "founder",
        "owner_address": wallet["address"], "status": "deployed",
        "source": template["source"], "functions": template["functions"],
        "state": {"calls": 0, "plan": "oneness-membership",
                  "price_usd_cents": 1199, "days": 30, "treasury": TREASURY}})
    monkeypatch.setenv("ONENESS_MEMBERSHIP_CONTRACT", row["id"])
    return {"contract": row, "wallet": wallet}


def test_the_contract_books_the_account_and_extends_it_on_renewal(
        sandbox, make_wallet, node_stub, fixed_rate, monkeypatch):
    deployed = _deploy_membership_contract(make_wallet, monkeypatch)
    identifier = deployed["contract"]["id"]
    address = deployed["wallet"]["address"]
    start = 1_000_000

    first = membership.activate(address, days=30, amount_micro=333_055_556,
                                txid="aa" * 32, now=start)
    assert first["function"] == "subscribe"
    assert first["contract"]["ok"] is True
    assert first["account"]["expires_at"] == start + 30 * 86_400

    state = contracts.CONTRACTS.get(identifier)["state"]
    account = state["accounts"][address]
    assert account["payments"] == 1 and account["paid_micro"] == 333_055_556
    assert account["last_txid"] == "aa" * 32
    assert state["collected_micro"] == 333_055_556

    # renewing inside the paid period extends it - no paid day is ever lost
    second = membership.activate(address, days=30, amount_micro=333_055_556,
                                 txid="bb" * 32, now=start + 86_400)
    assert second["function"] == "renew"
    assert second["account"]["expires_at"] == start + 60 * 86_400
    state = contracts.CONTRACTS.get(identifier)["state"]
    assert state["accounts"][address]["payments"] == 2
    assert state["collected_micro"] == 2 * 333_055_556

    # after expiry the next payment opens a fresh period, not a renewal
    third = membership.activate(address, days=30, amount_micro=333_055_556,
                                txid="cc" * 32, now=start + 61 * 86_400)
    assert third["function"] == "subscribe"
    assert third["account"]["expires_at"] == start + 91 * 86_400

    moment = start + 61 * 86_400 + 60        # one minute into the third period
    active = contracts.call(identifier, "active",
                            {"address": address, "now": moment}, admin=True)
    assert active["result"] is True
    expired = contracts.call(identifier, "expired",
                             {"address": address, "now": moment}, admin=True)
    assert expired["result"] is False
    later = contracts.call(identifier, "expired",
                           {"address": address,
                            "now": start + 92 * 86_400}, admin=True)
    assert later["result"] is True
    priced = contracts.call(identifier, "price", {"tor_usd_micro": 3_600_000},
                            admin=True)
    assert priced["result"] == 333_055_555     # the contract's own integer maths


def test_the_membership_template_is_a_real_rust_program():
    template = contracts.TEMPLATES["membership"]

    assert "pub fn call(" in template["source"]
    assert set(template["functions"]) == {"price", "subscribe", "renew", "cancel",
                                          "active", "expired"}

    compiled = contracts.compile_check(template["source"])
    if compiled.get("checked") is False:
        pytest.skip("rustc is not installed - compilation cannot be verified")
    assert compiled["ok"] is True, compiled.get("stderr")


# ----------------------------------------------------------------- the API
def test_the_api_quotes_opens_pays_and_reports(sandbox, make_wallet, node_stub,
                                               fixed_rate):
    wallet = make_wallet("founder")
    http = client()
    headers = _headers("founder", wallet["address"])

    overview = http.get("/api/payments/membership").get_json()["data"]
    assert overview["plan"]["usd"] == 11.99
    assert overview["quote"]["micro"] == 333_055_556
    assert overview["treasury"] == TREASURY
    assert overview["contract"]["deployed"] is False

    anonymous = http.post("/api/payments/membership/invoices",
                          json={"months": 1}, headers={})
    assert anonymous.status_code in (400, 401, 403)   # no wallet session, no invoice

    created = http.post("/api/payments/membership/invoices",
                        json={"months": 1}, headers=headers).get_json()["data"]
    assert created["invoice"]["id"] == "sub_0001"

    paid = http.post(f"/api/payments/membership/invoices/"
                     f"{created['invoice']['id']}/pay",
                     json={"privacy": "anonymous"},
                     headers=headers).get_json()["data"]
    assert paid["txid"] and paid["receipt"]["amount_tor"] == "3.33055556"
    assert paid["settlement"]["privacy"] == "anonymous"

    account = http.get(f"/api/payments/membership/accounts/"
                       f"{wallet['address']}").get_json()["data"]
    assert account["active"] is True and account["payments"] == 1

    receipts = http.get(f"/api/payments/membership/accounts/"
                        f"{wallet['address']}/receipts").get_json()["data"]
    assert receipts["count"] == 1
    assert receipts["totals"]["paid_micro"] == 333_055_556

    deployed = http.post("/api/payments/membership/contract", json={})
    assert deployed.status_code in (401, 403)          # admin only




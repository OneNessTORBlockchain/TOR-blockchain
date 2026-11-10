"""Per-transaction privacy: the wallet chooses PUBLIC or ANONYMOUS before a send.

The old rule was time-based (public for 30 days, then sealed private). The rule
these tests pin down is per transaction: a *public* send writes the wallet's
address on chain, an *anonymous* send writes ``Tor-Anonymous-onion`` from the
first confirmation, and every public read path applies that masking.

Only the Rust node's HTTP answers are stubbed (the suite must not need a running
ledger); signing, the ``external_ref`` stamp and the masking are production code.
"""
from __future__ import annotations

import time

import pytest

from oneness_tor.services import auth, ledger, node_sync, privacy, wallet_access


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def _headers(name: str, address: str) -> dict:
    session = wallet_access.open_session(name, address)
    return {wallet_access.HEADER: session["session"]}


#: A real TOR1…onion address (the node's validator is strict about the shape).
RECIPIENT = "TOR1" + "a" * 30 + "onion"


def _record(**over) -> dict:
    now = int(time.time())
    base = {"txid": "ab" * 32, "kind": "transfer", "from": "TOR1senderonion",
            "to": "TOR1receiveronion", "amount": 2 * 10 ** 8, "token": None,
            "fee": 0, "note": "memo", "external_ref": "", "signature": "",
            "seq": 7, "timestamp": now - 60, "height": 12,
            "block_hash": "cd" * 32, "visibility": "public"}
    base.update(over)
    return base


# ------------------------------------------------------------------ the mode
def test_normalize_accepts_every_spelling_and_rejects_a_typo():
    assert privacy.normalize("public") == "public"
    assert privacy.normalize("PUBLIC ") == "public"
    assert privacy.normalize("anonymous") == "anonymous"
    assert privacy.normalize("Anon") == "anonymous"
    assert privacy.normalize("private") == "anonymous"
    assert privacy.normalize("Tor-Anonymous-onion") == "anonymous"
    # nothing said means the node's default, which ships as public
    assert privacy.normalize(None) == "public"
    assert privacy.normalize("") == "public"

    with pytest.raises(Exception) as problem:
        privacy.normalize("anonimous")
    assert getattr(problem.value, "code", "") == "VALIDATION_ERROR"
    assert problem.value.details["accepted"] == ["public", "anonymous"]

    with pytest.raises(Exception):
        privacy.normalize(True)


def test_chain_ref_is_empty_for_public_and_round_trips_anonymous():
    assert privacy.chain_ref("public") == ""
    ref = privacy.chain_ref("anonymous")
    assert ref == "oneness-privacy:1|mode=anonymous"
    assert privacy.mode_of({"external_ref": ref}) == "anonymous"
    assert privacy.mode_of({"external_ref": ""}) == "public"
    # a record written by the node itself (no external_ref key at all)
    assert privacy.mode_of({"txid": "ab" * 32}) == "public"
    # tolerant of a hand-written ref and of a native privacy field
    assert privacy.mode_of({"external_ref": "privacy=anon"}) == "anonymous"
    assert privacy.mode_of({"external_ref": "notes about a mint"}) == "public"
    assert privacy.mode_of({"privacy": "anonymous"}) == "anonymous"


def test_public_view_masks_the_sender_and_keeps_everything_else(sandbox):
    record = _record(external_ref=privacy.chain_ref("anonymous"))
    shown = privacy.public_view(record)

    assert shown["from"] == privacy.ANONYMOUS_LABEL == "Tor-Anonymous-onion"
    assert shown["to"] == record["to"]          # the receiver stays visible
    assert shown["amount"] == record["amount"]  # amounts are public by design
    assert shown["txid"] == record["txid"]
    assert shown["privacy"] == "anonymous" and shown["anonymous"] is True
    assert shown["masked_fields"] == ["from"]
    # the original record is never mutated
    assert record["from"] == "TOR1senderonion"

    shown_public = privacy.public_view(_record())
    assert shown_public["from"] == "TOR1senderonion"
    assert shown_public["privacy"] == "public"
    assert shown_public["anonymous"] is False
    assert shown_public["masked_fields"] == []


def test_label_follows_config_and_the_receiver_is_never_masked(sandbox, monkeypatch):
    record = _record(external_ref=privacy.chain_ref("anonymous"))
    monkeypatch.setenv("ONENESS_PRIVACY_LABEL", "TOR-Anonymous-relay")
    shown = privacy.public_view(record)
    assert shown["from"] == "TOR-Anonymous-relay"
    # A sender can only hide its own address: the receiver always stays real,
    # whatever the config says — there is no recipient-masking switch at all.
    assert shown["to"] == "TOR1receiveronion"
    assert shown["masked_fields"] == ["from"]
    assert not hasattr(privacy, "mask_recipient")

    monkeypatch.setenv("ONENESS_PRIVACY_DEFAULT", "anonymous")
    assert privacy.default_mode() == "anonymous"
    assert privacy.normalize(None) == "anonymous"


# ------------------------------------------------------------------ the send
class FakeNode:
    """A stand-in for the ``core.node_client.node`` singleton.

    The suite's other modules patch methods on that singleton *instance*, and
    pytest's undo leaves the old bound method behind — a later class-level patch
    is then shadowed by it. Owning the whole object in these tests is the only
    order-independent way to answer the node.
    """

    def __init__(self, records: list[dict] | None = None):
        self.submitted: list[dict] = []
        self.records = list(records or [])

    def submit_tx(self, signed: dict) -> dict:
        self.submitted.append(signed)
        return {"ok": True, "data": {
            "accepted": 1, "height": 42, "head_hash": "ff" * 32,
            "records": [_record(txid=f"{len(self.submitted):02x}" * 32, **{
                "from": signed["from"], "to": signed["to"],
                "amount": signed["amount"], "note": signed["note"],
                "external_ref": signed.get("external_ref", ""),
                "signature": signed["signature"]})]}}

    def info(self) -> dict:
        return {"ok": True, "data": {
            "founder": "TOR1feereceiveronion", "fee_receiver": "TOR1feereceiveronion",
            "height": 42}}

    def fee_receiver(self) -> str:
        return "TOR1feereceiveronion"

    def account(self, address: str) -> dict:
        return {"ok": True, "data": {"address": address, "balance": 100.0,
                                     "balance_micro": 10_000_000_000, "nonce": 3,
                                     "tokens": {}, "role": "holder"}}

    def public_records(self, limit: int = 50) -> dict:
        return {"ok": True, "data": {"records": self.records[:limit]}}

    def events(self, limit: int = 50) -> dict:
        return {"ok": True, "data": {"events": self.records[:limit],
                                     "note": "public window"}}

    def tx(self, txid: str, token: str | None = None) -> dict:
        for record in self.records:
            if record.get("txid") == txid:
                return {"ok": True, "data": record}
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": txid}}


@pytest.fixture
def node_stub(monkeypatch):
    """Own the node client for this test: capture every signed transaction."""
    fake = FakeNode()
    monkeypatch.setattr(ledger, "node", fake)
    monkeypatch.setattr(node_sync, "is_replica", lambda: False)
    return fake.submitted


def test_send_anonymous_stamps_the_chain_and_labels_the_receipt(sandbox, make_wallet,
                                                               node_stub):
    wallet = make_wallet("founder")
    http = client()
    response = http.post("/api/wallets/founder/send",
                         json={"to": RECIPIENT, "amount": 1.5,
                               "privacy": "anonymous"},
                         headers=_headers("founder", wallet["address"]))
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]

    assert data["privacy"] == "anonymous" and data["anonymous"] is True
    assert data["on_chain_from"] == "Tor-Anonymous-onion"
    assert data["from"] == wallet["address"]        # the signer's own receipt
    # the transfer *and* its platform fee carry the mode: a public fee would
    # re-publish the sender the wallet just hid
    assert [tx["external_ref"] for tx in node_stub] == [
        "oneness-privacy:1|mode=anonymous"] * 2
    assert data["platform_fee"]["privacy"] == "anonymous"
    assert privacy.mode_of(node_stub[0]) == "anonymous"


def test_send_public_and_default_are_byte_identical_to_a_legacy_transfer(
        sandbox, make_wallet, node_stub):
    wallet = make_wallet("founder")
    http = client()
    for payload in ({"privacy": "public"}, {}):
        response = http.post("/api/wallets/founder/send",
                             json={"to": RECIPIENT, "amount": 1, **payload},
                             headers=_headers("founder", wallet["address"]))
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()["data"]
        assert data["privacy"] == "public" and data["anonymous"] is False
        assert data["on_chain_from"] == wallet["address"]
    assert [tx["external_ref"] for tx in node_stub] == ["", "", "", ""]


def test_a_typo_is_refused_before_anything_is_signed(sandbox, make_wallet, node_stub):
    wallet = make_wallet("founder")
    http = client()
    response = http.post("/api/wallets/founder/send",
                         json={"to": RECIPIENT, "amount": 1,
                               "privacy": "anonimous"},
                         headers=_headers("founder", wallet["address"]))
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert node_stub == []           # nothing was signed, nothing left the node


def test_sign_only_carries_the_mode_in_the_signed_tx(sandbox, make_wallet):
    wallet = make_wallet("founder")
    http = client()
    response = http.post("/api/wallets/founder/sign",
                         json={"to": RECIPIENT, "amount": 2,
                               "privacy": "anonymous"},
                         headers=_headers("founder", wallet["address"]))
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]
    assert data["privacy"] == "anonymous"
    assert data["on_chain_from"] == "Tor-Anonymous-onion"
    assert data["signed_tx"]["external_ref"] == "oneness-privacy:1|mode=anonymous"


# -------------------------------------------------------------- the public read
def test_public_records_and_tx_are_masked_but_the_operator_sees_the_truth(
        sandbox, monkeypatch):
    from oneness_tor.api import chain as chain_api

    hidden = _record(external_ref=privacy.chain_ref("anonymous"))
    fake = FakeNode([hidden])
    monkeypatch.setattr(ledger, "node", fake)
    monkeypatch.setattr(chain_api, "node", fake)
    monkeypatch.setattr(auth, "expected_hash", lambda: None)
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "test-admin-token")

    http = client()

    listed = http.get("/api/public/records?limit=5").get_json()["data"]
    assert listed["records"][0]["from"] == "Tor-Anonymous-onion"
    assert listed["records"][0]["anonymous"] is True
    assert listed["records"][0]["txid"] == hidden["txid"]
    assert "TOR1senderonion" not in str(listed)

    events = http.get("/api/chain/events?limit=5").get_json()["data"]
    assert events["events"][0]["from"] == "Tor-Anonymous-onion"

    single = http.get(f"/api/chain/tx/{hidden['txid']}").get_json()["data"]
    assert single["from"] == "Tor-Anonymous-onion"
    assert single["masked_fields"] == ["from"]

    truth = http.get(f"/api/chain/tx/{hidden['txid']}",
                     headers={"x-admin-token": "test-admin-token"}).get_json()["data"]
    assert truth["from"] == "TOR1senderonion"


def test_history_labels_the_row_and_shows_what_the_public_sees(sandbox, make_wallet,
                                                              monkeypatch):
    wallet = make_wallet("founder")
    hidden = _record(**{"from": wallet["address"], "to": "TOR1receiveronion",
                        "external_ref": privacy.chain_ref("anonymous")})
    plain = _record(txid="ee" * 32, **{"from": wallet["address"],
                                       "to": "TOR1receiveronion", "seq": 8})
    monkeypatch.setattr(ledger, "node", FakeNode([plain, hidden]))

    data = ledger.history("founder")
    rows = {row["txid"]: row for row in data["records"]}
    anonymous = rows[hidden["txid"]]
    assert anonymous["privacy"] == "anonymous" and anonymous["anonymous"] is True
    assert anonymous["from"] == wallet["address"]          # owner view
    assert anonymous["on_chain_from"] == "Tor-Anonymous-onion"
    assert rows[plain["txid"]]["anonymous"] is False
    assert rows[plain["txid"]]["on_chain_from"] == wallet["address"]


# --------------------------------------------------------------- the documents
def test_privacy_endpoint_publishes_the_rule(sandbox):
    body = client().get("/api/privacy")
    assert body.status_code == 200, body.get_data(as_text=True)
    document = body.get_json()["data"]

    assert document["standard"] == privacy.STANDARD == "oneness-tor/privacy/1"
    assert document["modes"] == ["public", "anonymous"]
    assert document["default_mode"] == "public"
    assert document["anonymous_label"] == "Tor-Anonymous-onion"
    assert document["chain_value_for_anonymous"] == \
        "oneness-privacy:1|mode=anonymous"
    assert document["hides"] == ["from (the sending wallet's own address)"]
    assert document["never_masked"] and "to (the receiver's address" in \
        document["never_masked"][0]
    # the honest half: masking an address is not untraceability
    assert "balances" in document["still_public"]
    assert "not untraceability" in document["not_cryptographic"]
    assert "/api/privacy" in client().get("/api/docs").get_data(as_text=True)


def test_the_old_window_documents_are_gone(sandbox):
    """No /api/policy, no sweep, and no window key in the privacy document."""
    http = client()
    assert http.get("/api/policy").status_code == 404
    assert http.post("/api/policy/sweep", json={}).status_code == 404
    document = http.get("/api/privacy").get_json()["data"]
    assert "window_days" not in document and "mask_recipient" not in document

"""The seam the three paid ANONYMOUS products use: proof, membership, free tier.

Only the Rust node's HTTP answers are stubbed -- the Ed25519 signatures, the
canonical string, the nonce window, the invoice, the settlement checks, the
contract call and the free-message counter are production code.
"""
from __future__ import annotations

import json
import secrets
import time

import base58
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from oneness_tor.services import (contracts, ledger, membership, node_sync,
                                  wallet_proof)

TREASURY = "TOR1" + "t" * 30 + "onion"
OTHER = "TOR1" + "o" * 30 + "onion"
PRICE_MICRO = 333_055_556          # $11.99 at $3.60 / TOR


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def signed_headers(wallet: dict, method: str, path: str, body=None, *,
                   timestamp: int | None = None, nonce: str | None = None,
                   key=None) -> dict:
    """The four headers a product sends, signed by a real Ed25519 key."""
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    stamp = int(timestamp if timestamp is not None else time.time())
    nonce = nonce or base58.b58encode(secrets.token_bytes(16)).decode("ascii")
    message = wallet_proof.canonical(method, path, stamp, nonce,
                                     wallet_proof.body_hash(raw))
    private = key or Ed25519PrivateKey.from_private_bytes(
        base58.b58decode(wallet["secret_key_base58"])[:32])
    signature = base58.b58encode(private.sign(message.encode("utf-8")))
    return {wallet_proof.WALLET_HEADER: wallet["pubkey_base58"],
            wallet_proof.TIMESTAMP_HEADER: str(stamp),
            wallet_proof.NONCE_HEADER: nonce,
            wallet_proof.SIGNATURE_HEADER: signature.decode("ascii")}


def post(http, path: str, wallet: dict, body=None, **kwargs):
    """POST a JSON body with the wallet's signature over exactly those bytes."""
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = signed_headers(wallet, "POST", path, body, **kwargs)
    headers["content-type"] = "application/json"
    return http.post(path, data=raw, headers=headers)


class FakeNode:
    """The Rust node's HTTP answers, with every accepted record readable back."""

    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.records: dict[str, dict] = {}
        self.overrides: dict[str, dict] = {}

    def submit_tx(self, signed: dict) -> dict:
        self.submitted.append(signed)
        txid = (f"{len(self.submitted):02x}" * 32)
        record = {"txid": txid, "kind": signed.get("kind"),
                  "from": signed["from"], "to": signed["to"],
                  "amount": int(signed["amount"]), "token": signed.get("token"),
                  "fee": 0, "note": signed.get("note") or "",
                  "external_ref": signed.get("external_ref") or "",
                  "signature": signed.get("signature") or "",
                  "timestamp": int(signed.get("timestamp") or 0),
                  "height": 7, "seq": len(self.submitted),
                  "block_hash": "ab" * 32}
        self.records[txid] = record
        return {"ok": True, "data": {"accepted": 1, "height": 7,
                                     "head_hash": "ff" * 32,
                                     "records": [record]}}

    def tx(self, txid: str, token: str | None = None) -> dict:
        if txid in self.overrides:
            return {"ok": True, "data": self.overrides[txid]}
        record = self.records.get(txid)
        if not record:
            return {"ok": False, "data": None,
                    "error": {"code": "NOT_FOUND", "message": "no such txid"}}
        return {"ok": True, "data": record}

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
    wallet_proof.reset()
    for name in ("ONENESS_MEMBERSHIP_CONTRACT", "ONENESS_MEMBERSHIP_USD",
                 "ONENESS_MEMBERSHIP_DAYS", "ONENESS_MEMBERSHIP_TREASURY",
                 "ONENESS_MEMBERSHIP_PLAN", "ONENESS_MEMBERSHIP_LABEL",
                 "ONENESS_MEMBERSHIP_FREE"):
        monkeypatch.delenv(name, raising=False)
    return fake


@pytest.fixture
def fixed_rate(monkeypatch):
    """Pin TOR at the published anchor: $3.60 -> 333 055 556 microTOR."""
    monkeypatch.setattr(membership, "tor_usd", lambda: 3.60)
    return 3.60


# ------------------------------------------------------- the wallet's proof
def test_a_valid_signature_proves_the_wallet_and_a_replay_does_not(make_wallet,
                                                                  node_stub):
    wallet = make_wallet("founder")
    body = {"product": "webapp"}
    headers = signed_headers(wallet, "POST", "/membership/x/use-free", body)

    proved = wallet_proof.prove(headers=headers, method="POST",
                                path="/membership/x/use-free",
                                raw_body=json.dumps(body).encode())
    assert proved["address"] == wallet["address"]
    assert proved["pubkey_base58"] == wallet["pubkey_base58"]

    with pytest.raises(Exception) as replay:
        wallet_proof.prove(headers=headers, method="POST",
                           path="/membership/x/use-free",
                           raw_body=json.dumps(body).encode())
    assert getattr(replay.value, "http_status", None) == 401
    assert "already been used" in str(replay.value)


def test_a_broken_signature_a_stale_clock_and_a_changed_body_are_all_401(
        make_wallet, node_stub):
    wallet = make_wallet("founder")
    path = "/membership/x/invoices"
    body = {"months": 1}

    broken = signed_headers(wallet, "POST", path, body)
    broken[wallet_proof.SIGNATURE_HEADER] = base58.b58encode(
        secrets.token_bytes(64)).decode()
    with pytest.raises(Exception) as bad:
        wallet_proof.prove(headers=broken, method="POST", path=path,
                           raw_body=json.dumps(body).encode())
    assert getattr(bad.value, "http_status", None) == 401

    stale = signed_headers(wallet, "POST", path, body,
                           timestamp=int(time.time()) - 600)
    with pytest.raises(Exception) as old:
        wallet_proof.prove(headers=stale, method="POST", path=path,
                           raw_body=json.dumps(body).encode())
    assert "clock" in str(old.value)

    good = signed_headers(wallet, "POST", path, body)
    with pytest.raises(Exception):
        wallet_proof.prove(headers=good, method="POST", path=path,
                           raw_body=json.dumps({"months": 12}).encode())


def test_the_wallet_verify_route_answers_valid_and_address(make_wallet, node_stub):
    wallet = make_wallet("founder")
    http = client()
    message = wallet_proof.canonical("POST", "/wallet/verify", 1_700_000_000,
                                     "nonce", wallet_proof.body_hash(b""))
    private = Ed25519PrivateKey.from_private_bytes(
        base58.b58decode(wallet["secret_key_base58"])[:32])
    signature = base58.b58encode(private.sign(message.encode())).decode()

    good = http.post("/wallet/verify", json={"pubkey": wallet["pubkey_base58"],
                                             "message": message,
                                             "signature": signature}).get_json()
    assert good["data"]["valid"] is True
    assert good["data"]["address"] == wallet["address"]

    wrong = http.post("/wallet/verify", json={"pubkey": wallet["pubkey_base58"],
                                              "message": message + "!",
                                              "signature": signature}).get_json()
    assert wrong["data"]["valid"] is False
    assert "address" not in wrong["data"]


# ------------------------------------------------------- membership() reads
def test_identity_reports_the_price_the_free_tier_and_the_contract(
        make_wallet, node_stub, fixed_rate):
    wallet = make_wallet("founder")
    data = client().get(f"/membership/{wallet['address']}").get_json()["data"]

    assert data["active"] is False and data["member"] is False
    assert data["price_usd"] == 11.99 and data["price_days"] == 30
    assert data["currency"] == "TOR" and data["settled_in"] == "TOR"
    assert data["free_messages_limit"] == 5
    assert data["free_messages_left"] == 5
    assert data["until"] is None
    assert data["quote"]["micro"] == PRICE_MICRO
    assert data["treasury"] == TREASURY
    assert data["contract"]["deployed"] is False
    assert data["plan"] == "oneness-membership"


# ------------------------------------------------------ the five free messages
def test_five_free_messages_per_wallet_then_402(make_wallet, node_stub):
    wallet = make_wallet("founder")
    other = make_wallet("second")
    http = client()
    path = f"/membership/{wallet['address']}/use-free"

    left = []
    for index in range(5):
        answer = post(http, path, wallet, {"product": "webapp",
                                           "note": f"turn {index}"})
        assert answer.status_code == 200, answer.get_json()
        left.append(answer.get_json()["data"]["left"])
    assert left == [4, 3, 2, 1, 0]

    sixth = post(http, path, wallet, {"product": "webapp"})
    assert sixth.status_code == 402
    error = sixth.get_json()["error"]
    assert error["code"] == "MEMBERSHIP_REQUIRED"
    assert error["details"]["price_usd"] == 11.99
    assert error["details"]["subscribe"].endswith("/invoices")

    # the counter is per wallet, and the chain keeps the audit trail
    fresh = post(http, f"/membership/{other['address']}/use-free", other,
                 {"product": "webapp"})
    assert fresh.get_json()["data"]["left"] == 4
    state = membership.free_state(wallet["address"])
    assert state["used"] == 5 and state["exhausted"] is True
    assert len(state["turns"]) == 5


def test_an_unsigned_or_foreign_call_cannot_spend_a_free_message(make_wallet,
                                                                node_stub):
    wallet = make_wallet("founder")
    other = make_wallet("second")
    http = client()
    path = f"/membership/{wallet['address']}/use-free"

    unsigned = http.post(path, json={"product": "webapp"})
    assert unsigned.status_code == 401

    foreign = post(http, path, other, {"product": "webapp"})
    assert foreign.status_code == 403
    assert membership.free_state(wallet["address"])["used"] == 0


# ----------------------------------------------------------- the real money
def _invoice(http, wallet: dict, **body) -> dict:
    path = f"/membership/{wallet['address']}/invoices"
    answer = post(http, path, wallet, {"months": 1, "product": "webapp", **body})
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()["data"]


def _settle(http, wallet: dict, invoice_id: str, txid: str):
    return post(http, f"/membership/{wallet['address']}/settle", wallet,
                {"invoice_id": invoice_id, "txid": txid})


def test_a_real_payment_activates_only_after_the_transfer_is_verified(
        make_wallet, node_stub, fixed_rate):
    wallet = make_wallet("founder")
    http = client()
    opened = _invoice(http, wallet)

    invoice = opened["invoice"]
    assert invoice["micro"] == PRICE_MICRO
    assert invoice["amount_tor"] == "3.33055556"
    assert invoice["treasury"] == TREASURY
    assert opened["memo"] == f"membership {invoice['id']}"
    assert opened["settle"]["path"].endswith("/settle")

    # nothing is active before the transfer exists
    before = http.get(f"/membership/{wallet['address']}").get_json()["data"]
    assert before["active"] is False

    missing = _settle(http, wallet, invoice["id"], "00" * 32)
    assert missing.status_code == 400
    assert "not on this node" in missing.get_json()["error"]["message"]

    # a real signed Ed25519 transfer, exactly as the product would send it
    settlement = ledger.send_micro("founder", invoice["treasury"],
                                   invoice["micro"], note=opened["memo"])
    paid = _settle(http, wallet, invoice["id"], settlement["txid"])
    assert paid.status_code == 200, paid.get_json()
    body = paid.get_json()["data"]
    assert body["idempotent"] is False
    assert body["receipt"]["amount_tor"] == "3.33055556"
    assert body["payment"]["sent_micro"] == PRICE_MICRO
    assert body["membership"]["function"] == "subscribe"
    assert body["membership"]["account"]["payments"] == 1

    after = http.get(f"/membership/{wallet['address']}").get_json()["data"]
    assert after["active"] is True and after["member"] is True
    assert after["until"].endswith("+00:00")
    assert after["days_left"] > 29 and after["free_messages_left"] == 5
    assert after["payments"] == 1 and after["last_txid"] == settlement["txid"]

    receipts = http.get(
        f"/membership/{wallet['address']}/receipts").get_json()["data"]
    assert receipts["count"] == 1
    assert receipts["totals"]["paid_micro"] == PRICE_MICRO


def test_settle_is_idempotent_and_one_transfer_cannot_buy_two_periods(
        make_wallet, node_stub, fixed_rate):
    wallet = make_wallet("founder")
    http = client()
    first = _invoice(http, wallet)
    payment = ledger.send_micro("founder", first["invoice"]["treasury"],
                                PRICE_MICRO, note=first["memo"])
    opening = _settle(http, wallet, first["invoice"]["id"], payment["txid"])
    expires = opening.get_json()["data"]["membership"]["account"]["expires_at"]

    again = _settle(http, wallet, first["invoice"]["id"], payment["txid"])
    assert again.status_code == 200
    replay = again.get_json()["data"]
    assert replay["idempotent"] is True
    assert replay["membership"]["account"]["expires_at"] == expires
    assert replay["membership"]["account"]["payments"] == 1

    second = _invoice(http, wallet)
    stolen = _settle(http, wallet, second["invoice"]["id"], payment["txid"])
    assert stolen.status_code == 400
    assert "already settled" in stolen.get_json()["error"]["message"]

    # a *new* transfer renews the running period instead of restarting it
    renewed_payment = ledger.send_micro("founder", second["invoice"]["treasury"],
                                        PRICE_MICRO, note=second["memo"])
    renewed = _settle(http, wallet, second["invoice"]["id"],
                      renewed_payment["txid"]).get_json()["data"]
    assert renewed["membership"]["function"] == "renew"
    assert renewed["membership"]["account"]["expires_at"] == expires + 30 * 86_400
    assert renewed["membership"]["account"]["payments"] == 2


def test_a_short_a_misdirected_or_foreign_payment_is_refused(make_wallet, node_stub,
                                                            fixed_rate):
    wallet = make_wallet("founder")
    other = make_wallet("second")
    http = client()
    opened = _invoice(http, wallet)
    invoice = opened["invoice"]
    payment = ledger.send_micro("founder", invoice["treasury"], PRICE_MICRO,
                                note=opened["memo"])
    record = node_stub.records[payment["txid"]]

    node_stub.overrides[payment["txid"]] = {**record, "to": OTHER}
    wrong_wallet = _settle(http, wallet, invoice["id"], payment["txid"])
    assert wrong_wallet.status_code == 400
    assert "treasury" in wrong_wallet.get_json()["error"]["message"]

    node_stub.overrides[payment["txid"]] = {**record, "amount": PRICE_MICRO - 1}
    short = _settle(http, wallet, invoice["id"], payment["txid"])
    assert short.status_code == 400
    assert "microTOR" in short.get_json()["error"]["message"]

    node_stub.overrides[payment["txid"]] = {**record, "token": "MEME"}
    token = _settle(http, wallet, invoice["id"], payment["txid"])
    assert token.status_code == 400
    assert "TOR" in token.get_json()["error"]["message"]

    node_stub.overrides[payment["txid"]] = {**record, "from": other["address"]}
    foreign = _settle(http, wallet, invoice["id"], payment["txid"])
    assert foreign.status_code == 403
    assert "another wallet" in foreign.get_json()["error"]["message"]

    node_stub.overrides.pop(payment["txid"])
    still_open = http.get(f"/membership/{wallet['address']}").get_json()["data"]
    assert still_open["active"] is False


def test_an_unsigned_or_foreign_settle_never_activates_anything(make_wallet,
                                                              node_stub,
                                                              fixed_rate):
    wallet = make_wallet("founder")
    other = make_wallet("second")
    http = client()
    opened = _invoice(http, wallet)
    payment = ledger.send_micro("founder", opened["invoice"]["treasury"],
                                PRICE_MICRO, note=opened["memo"])

    unsigned = http.post(f"/membership/{wallet['address']}/settle",
                         json={"invoice_id": opened["invoice"]["id"],
                               "txid": payment["txid"]})
    assert unsigned.status_code == 401

    foreign = post(http, f"/membership/{wallet['address']}/settle", other,
                   {"invoice_id": opened["invoice"]["id"],
                    "txid": payment["txid"]})
    assert foreign.status_code == 403
    assert http.get(f"/membership/{wallet['address']}").get_json()["data"][
        "active"] is False


def test_expiration_and_cancellation_close_the_period(make_wallet, node_stub,
                                                     fixed_rate):
    wallet = make_wallet("founder")
    http = client()
    opened = _invoice(http, wallet)
    payment = ledger.send_micro("founder", opened["invoice"]["treasury"],
                                PRICE_MICRO, note=opened["memo"])
    _settle(http, wallet, opened["invoice"]["id"], payment["txid"])
    expires = http.get(f"/membership/{wallet['address']}").get_json()["data"][
        "expires_at"]

    # the period is exactly 30 days: one second past it, the wallet is not a member
    lapsed = membership.identity(wallet["address"], now=expires + 1)
    assert lapsed["active"] is False and lapsed["days_left"] == 0
    inside = membership.identity(wallet["address"], now=expires - 60)
    assert inside["active"] is True

    cancelled = post(http, f"/membership/{wallet['address']}/cancel", wallet,
                     {"reason": "operator asked"}).get_json()["data"]
    assert cancelled["cancelled"] is True
    assert cancelled["refund"] == "none"
    assert cancelled["contract"]["deployed"] is False   # nothing deployed on this node
    after = http.get(f"/membership/{wallet['address']}").get_json()["data"]
    assert after["active"] is False
    assert after["payments"] == 1

    missing = post(http, f"/membership/{OTHER}/cancel", wallet, {})
    assert missing.status_code == 403


def test_the_free_tier_and_the_paid_tier_are_separate_facts(make_wallet, node_stub,
                                                           fixed_rate):
    wallet = make_wallet("founder")
    http = client()
    for _ in range(5):
        assert post(http, f"/membership/{wallet['address']}/use-free", wallet,
                    {"product": "api"}).status_code == 200
    exhausted = http.get(f"/membership/{wallet['address']}").get_json()["data"]
    assert exhausted["free_messages_left"] == 0
    assert exhausted["active"] is False

    opened = _invoice(http, wallet)
    payment = ledger.send_micro("founder", opened["invoice"]["treasury"],
                                PRICE_MICRO, note=opened["memo"])
    _settle(http, wallet, opened["invoice"]["id"], payment["txid"])
    paying = http.get(f"/membership/{wallet['address']}").get_json()["data"]
    # a paid member keeps the same five-message fact: paying does not refill it
    assert paying["active"] is True and paying["free_messages_left"] == 0



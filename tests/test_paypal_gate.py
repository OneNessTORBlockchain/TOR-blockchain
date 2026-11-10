"""The PayPal gate: a closed gate says so, and a settled order moves real TOR.

No test here reaches PayPal or the ledger: the HTTP layer and the transfer are
replaced, so every assertion is about this node's own logic.
"""
from __future__ import annotations

import pytest

from core.errors import AppError, ValidationError
from oneness_tor.services import paypal_gate

BUYER = "TOR12xd7zywc3iuwcls3fqzsozkgo5ahv3gnonion"
TREASURY = "TOR1ski5tu2bg7lh3uc7wh4xl37qmxswz5skonion"


@pytest.fixture
def gate(monkeypatch, sandbox):
    """A configured gate whose HTTPS layer is a list of canned replies."""
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("ONENESS_TOR_TREASURY_WALLET", TREASURY)
    monkeypatch.delenv("PAYPAL_ENV", raising=False)
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    calls: list[dict] = []

    def fake_http(method, path, *, body=None, token=None, auth=None):
        calls.append({"method": method, "path": path, "body": body,
                      "token": token, "auth": auth})
        if path == "/v1/oauth2/token":
            return {"access_token": "token-123"}
        if path == "/v2/checkout/orders":
            return {"id": "ORDER-1",
                    "links": [{"rel": "approve",
                               "href": "https://paypal.test/approve/ORDER-1"}]}
        if path.endswith("/capture"):
            return {"status": "COMPLETED", "purchase_units": [{
                "payments": {"captures": [{"id": "CAP-1",
                                           "amount": {"value": "36.00",
                                                      "currency_code": "USD"}}]}}]}
        return {}

    monkeypatch.setattr(paypal_gate, "_http", fake_http)
    return {"calls": calls, "monkeypatch": monkeypatch}


def test_an_unconfigured_gate_is_closed_and_names_what_is_missing(monkeypatch,
                                                                  sandbox):
    for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET",
                 "ONENESS_TOR_TREASURY_WALLET", "PAYPAL_ENV"):
        monkeypatch.delenv(name, raising=False)
    assert paypal_gate.status()["gate"] == "closed"
    with pytest.raises(AppError) as refused:
        paypal_gate.create_buy(1, BUYER)
    assert refused.value.code == "PAYPAL_NOT_CONFIGURED"
    assert "PAYPAL_CLIENT_ID" in refused.value.details["missing"]
    assert refused.value.http_status == 503


def test_a_configured_gate_quotes_at_the_platform_anchor(gate):
    quote = paypal_gate.quote(10)
    assert quote["price_usd"] == pytest.approx(paypal_gate.price_usd())
    assert quote["usd"] == pytest.approx(10 * quote["price_usd"], abs=0.01)
    assert quote["micro_tor"] == 10 * 10 ** 8
    with pytest.raises(ValidationError):
        paypal_gate.quote(0)


def test_create_buy_asks_paypal_for_exactly_the_quoted_amount(gate):
    result = paypal_gate.create_buy(10, BUYER, note="first buy")
    order_call = next(call for call in gate["calls"]
                      if call["path"] == "/v2/checkout/orders")
    unit = order_call["body"]["purchase_units"][0]["amount"]
    assert unit["currency_code"] == "USD"
    assert unit["value"] == f"{result['order']['usd']:.2f}"
    assert order_call["body"]["intent"] == "CAPTURE"
    assert result["approve_url"] == "https://paypal.test/approve/ORDER-1"
    assert paypal_gate._order_row("ORDER-1")["status"] == "created"
    assert "secret" not in str(result).lower()


def test_capture_sends_the_tor_from_the_treasury_and_is_idempotent(gate):
    sent: list[dict] = []

    def fake_send(name, to, micro, *, note="", **kwargs):
        sent.append({"name": name, "to": to, "micro": micro, "note": note})
        return {"txid": "abc123", "height": 967}

    gate["monkeypatch"].setattr(paypal_gate.ledger, "send_micro", fake_send)
    paypal_gate.create_buy(10, BUYER)
    settled = paypal_gate.capture_buy("ORDER-1")
    assert settled["settled"] is True and settled["txid"] == "abc123"
    assert sent == [{"name": TREASURY, "to": BUYER,
                     "micro": 10 * 10 ** 8, "note": "paypal buy ORDER-1"}]

    again = paypal_gate.capture_buy("ORDER-1")
    assert again["already"] is True
    assert len(sent) == 1                      # never twice
    assert len([call for call in gate["calls"]
                if call["path"].endswith("/capture")]) == 1


def test_an_underpaid_capture_never_sends_the_tor(gate):
    def short(method, path, **kwargs):
        if path.endswith("/capture"):
            return {"status": "COMPLETED", "purchase_units": [{
                "payments": {"captures": [{"id": "CAP-2",
                                           "amount": {"value": "1.00"}}]}}]}
        return {"access_token": "t"} if "oauth2" in path else {"id": "ORDER-1"}

    gate["monkeypatch"].setattr(paypal_gate, "_http", short)
    gate["monkeypatch"].setattr(
        paypal_gate.ledger, "send_micro",
        lambda *a, **k: pytest.fail("must not send when the money is short"))
    paypal_gate.create_buy(10, BUYER)
    with pytest.raises(AppError) as refused:
        paypal_gate.capture_buy("ORDER-1")
    assert refused.value.code == "PAYPAL_UNDERPAID"
    assert paypal_gate._order_row("ORDER-1")["status"] == "underpaid"


def test_money_taken_but_not_delivered_is_visible_not_hidden(gate):
    def refuse_to_send(*args, **kwargs):
        raise AppError("treasury is empty", code="CHAIN_UNAVAILABLE")

    gate["monkeypatch"].setattr(paypal_gate.ledger, "send_micro", refuse_to_send)
    paypal_gate.create_buy(10, BUYER)
    with pytest.raises(AppError) as refused:
        paypal_gate.capture_buy("ORDER-1")
    assert refused.value.code == "PAYPAL_UNDELIVERED"
    row = paypal_gate._order_row("ORDER-1")
    assert row["status"] == "paid_undelivered"
    assert "treasury is empty" in row["problem"]


def test_a_sell_names_the_exact_deposit_and_pays_nothing_yet(gate):
    result = paypal_gate.start_sell(5, "seller-wallet", note="cash out")
    order = result["order"]
    assert order["status"] == "awaiting_tor"
    assert order["deposit_address"] == TREASURY
    assert order["deposit_note"] == f"paypal-sell:{order['id']}"
    assert order["micro_tor"] == 5 * 10 ** 8
    assert any("deposit" in line.lower() or "send" in line.lower()
               for line in result["how"])


def test_settle_sell_only_marks_a_payout_against_the_proved_deposit(gate):
    order = paypal_gate.start_sell(5, "seller-wallet")["order"]

    def history(address, **kwargs):
        return {"records": [{"txid": "dep-1", "from": "seller-wallet",
                             "amount": 5 * 10 ** 8,
                             "note": order["deposit_note"]}]}

    gate["monkeypatch"].setattr(paypal_gate.ledger, "history", history)
    settled = paypal_gate.settle_sell(order["id"], "dep-1")
    assert settled["payout_due"] is True
    assert settled["order"]["status"] == "payout_due"
    assert settled["order"]["txid"] == "dep-1"
    assert paypal_gate.settle_sell(order["id"], "dep-1")["already"] is True

    unknown = paypal_gate.start_sell(5, "seller-wallet")["order"]
    with pytest.raises(ValidationError, match="not in the treasury"):
        paypal_gate.settle_sell(unknown["id"], "nope")

    short = paypal_gate.start_sell(5, "seller-wallet")["order"]

    def short_history(address, **kwargs):
        return {"records": [{"txid": "dep-2", "from": "seller-wallet",
                             "amount": 1}]}

    gate["monkeypatch"].setattr(paypal_gate.ledger, "history", short_history)
    with pytest.raises(ValidationError, match="amount"):
        paypal_gate.settle_sell(short["id"], "dep-2")


def test_a_webhook_is_data_until_paypal_verifies_it(gate):
    body = b'{"event_type":"CHECKOUT.ORDER.APPROVED"}'
    with pytest.raises(AppError) as refused:
        paypal_gate.verify_webhook({}, body)
    assert refused.value.code == "PAYPAL_WEBHOOK_UNCONFIGURED"

    gate["monkeypatch"].setenv("PAYPAL_WEBHOOK_ID", "WH-1")
    gate["monkeypatch"].setattr(
        paypal_gate, "_http",
        lambda method, path, **k: ({"access_token": "t"}
                                   if "oauth2" in path else
                                   {"verification_status": "SUCCESS"}))
    verified = paypal_gate.verify_webhook({"paypal-transmission-id": "t"}, body)
    assert verified["verified"] is True
    assert verified["type"] == "CHECKOUT.ORDER.APPROVED"

    gate["monkeypatch"].setattr(
        paypal_gate, "_http",
        lambda method, path, **k: ({"access_token": "t"}
                                   if "oauth2" in path else
                                   {"verification_status": "FAILURE"}))
    with pytest.raises(AppError) as refused:
        paypal_gate.verify_webhook({"paypal-transmission-id": "t"}, body)
    assert refused.value.code == "PAYPAL_WEBHOOK_INVALID"


def test_the_routes_are_wired_and_stay_closed_without_credentials(sandbox,
                                                                  monkeypatch):
    for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET",
                 "ONENESS_TOR_TREASURY_WALLET", "PAYPAL_ENV"):
        monkeypatch.delenv(name, raising=False)
    from oneness_tor.app import create_app

    http = create_app().test_client()
    status = http.get("/api/paypal/status")
    assert status.status_code == 200
    assert status.json["data"]["gate"] == "closed"
    buy = http.post("/api/paypal/buy", json={"tor": 1, "wallet": BUYER})
    assert buy.status_code == 503
    assert buy.json["error"]["code"] == "PAYPAL_NOT_CONFIGURED"
    assert http.post("/api/paypal/sell", json={"tor": 1}).status_code == 403


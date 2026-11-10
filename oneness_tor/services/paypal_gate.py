"""The PayPal gate: fiat in for TOR, TOR in for fiat - and no coin is minted.

Why a *gate* and not an exchange
--------------------------------
This ledger has **no mint function**. Supply is fixed at genesis
(``rust/torchain/src/chain.rs``), so a PayPal purchase cannot issue TOR: it can
only move TOR that a treasury wallet already holds. That is a treasury sale, and
pretending otherwise would be inventing coins on somebody's balance sheet.

* **BUY** - PayPal captures USD, then this node sends TOR **from the treasury
  wallet** to the buyer's address. If the treasury cannot pay, the order is
  marked ``paid_undelivered`` and the buyer's money is visibly owed, never
  silently kept.
* **SELL** - the seller sends TOR to the treasury address with the order's exact
  note, and the operator pays out from PayPal against that on-chain deposit
  (``settle_sell`` verifies the deposit in ``ledger.history`` before it marks the
  payout due).

Configuration is environment only, and no secret is ever read from a file in the
repository: ``.env`` is tracked in this repo, so putting a PayPal secret there
would commit it.

    PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET   the REST app's credentials
    PAYPAL_ENV=sandbox|live                   which PayPal (default sandbox)
    PAYPAL_WEBHOOK_ID                         enables signature verification
    ONENESS_TOR_TREASURY_WALLET               the wallet that holds the TOR
    PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL     where the buyer lands afterwards

Unset credentials are a **closed** gate: every call answers 503
``PAYPAL_NOT_CONFIGURED`` naming the variable that is missing. A store that
cannot take money must say so, not half-charge somebody.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.config import settings
from core.errors import AppError, ValidationError
from core.validator import format_tor, to_micro, validate_address
from oneness_tor.services import ledger
from oneness_tor.services.store import Collection, data_dir

ORDERS = Collection(lambda: data_dir("payments") / "paypal.json", "ppo")

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"
TIMEOUT = 30.0


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def client_id() -> str:
    return _env("PAYPAL_CLIENT_ID")


def client_secret() -> str:
    return _env("PAYPAL_CLIENT_SECRET")


def environment() -> str:
    """``sandbox`` unless ``PAYPAL_ENV=live`` - sandbox is never called live."""
    return "live" if _env("PAYPAL_ENV").lower() == "live" else "sandbox"


def base_url() -> str:
    return LIVE if environment() == "live" else SANDBOX


def treasury_wallet() -> str:
    return _env("ONENESS_TOR_TREASURY_WALLET")


def configured() -> bool:
    return bool(client_id() and client_secret())


def require_configured() -> None:
    """Fail closed, naming exactly what an operator still has to set."""
    missing = [name for name, value in (
        ("PAYPAL_CLIENT_ID", client_id()),
        ("PAYPAL_CLIENT_SECRET", client_secret()),
        ("ONENESS_TOR_TREASURY_WALLET", treasury_wallet())) if not value]
    if missing:
        raise AppError(
            "the PayPal gate is not configured - no money is taken and no TOR is "
            "sold until it is",
            code="PAYPAL_NOT_CONFIGURED", http_status=503,
            details={"missing": missing, "environment": environment(),
                     "gate": "/api/paypal/buy"})


def price_usd() -> float:
    """The TOR/USD anchor the platform already quotes (``pricing.tor_usd``)."""
    try:
        value = float(settings.get("pricing.tor_usd") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        raise AppError(
            "no TOR/USD price is configured (pricing.tor_usd / "
            "ONENESS_TOR_USD), so a fiat order cannot be priced",
            code="PRICE_UNAVAILABLE", http_status=503)
    return value


def quote(tor: float) -> dict:
    """Price a quantity of TOR in USD, at the anchor, with both sides shown."""
    amount = float(tor or 0)
    if amount <= 0:
        raise ValidationError("tor must be greater than zero",
                              details={"tor": tor})
    rate = price_usd()
    return {"tor": round(amount, 8), "usd": round(amount * rate, 2),
            "price_usd": rate, "micro_tor": to_micro(amount),
            "amount_tor": format_tor(to_micro(amount))}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http(method: str, path: str, *, body: dict | None = None,
          token: str | None = None, auth: tuple[str, str] | None = None) -> dict:
    """One HTTPS call. Split out so a test replaces it without a network."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"accept": "application/json"}
    if data is not None:
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    if auth:
        raw = f"{auth[0]}:{auth[1]}".encode("utf-8")
        headers["authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    request = urllib.request.Request(base_url() + path, data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as problem:
        detail = problem.read().decode("utf-8", "replace")[:400]
        raise AppError(f"PayPal refused {method} {path}: HTTP {problem.code}",
                       code="PAYPAL_HTTP_ERROR", http_status=502,
                       details={"status": problem.code, "detail": detail})
    except urllib.error.URLError as problem:
        raise AppError(
            f"PayPal is unreachable ({problem.reason}) - the gate stays closed",
            code="PAYPAL_UNREACHABLE", http_status=502, details={"path": path})
    return json.loads(text) if text.strip() else {}


def access_token() -> str:
    """A client_credentials token, minted per call (never cached on disk)."""
    require_configured()
    reply = _http("POST", "/v1/oauth2/token",
                  auth=(client_id(), client_secret()))
    token = str(reply.get("access_token") or "")
    if not token:
        raise AppError("PayPal returned no access token",
                       code="PAYPAL_HTTP_ERROR", http_status=502)
    return token


def status() -> dict:
    """What a widget may show - configured or not, and never a secret."""
    try:
        rate = price_usd()
    except AppError:
        rate = None
    open_gate = configured() and bool(treasury_wallet())
    return {"configured": configured(), "environment": environment(),
            "treasury": treasury_wallet(), "treasury_set": bool(treasury_wallet()),
            "webhook": bool(_env("PAYPAL_WEBHOOK_ID")),
            "price_usd": rate,
            "return_url": _env("PAYPAL_RETURN_URL"),
            "cancel_url": _env("PAYPAL_CANCEL_URL"),
            "gate": "open" if open_gate else "closed",
            "note": ("a buy is a treasury sale: TOR leaves a wallet that already "
                     "holds it, because this chain cannot mint")}


# --------------------------------------------------------------------- buying
def _order_row(order_id: str) -> dict:
    for row in ORDERS.all():
        if str(row.get("order_id") or "") == order_id:
            return row
    raise ValidationError(f"no PayPal order {order_id!r} started here",
                          details={"order_id": order_id})


def create_buy(tor: float, wallet: str, *, note: str = "") -> dict:
    """Start a BUY: a real PayPal order, a stored intent, nothing charged yet."""
    require_configured()
    address = str(validate_address(wallet) or wallet).strip()
    quote_row = quote(tor)
    if quote_row["usd"] < 0.01:
        raise ValidationError("the order is under one cent",
                              details={"usd": quote_row["usd"]})
    token = access_token()
    returned = _env("PAYPAL_RETURN_URL") or None
    cancelled = _env("PAYPAL_CANCEL_URL") or None
    body = {"intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "USD",
                           "value": f"{quote_row['usd']:.2f}"},
                "description": f"{quote_row['amount_tor']} TOR"[:127],
                "custom_id": address[:127],
            }],
            "application_context": {"brand_name": "OneNess TOR",
                                    "shipping_preference": "NO_SHIPPING",
                                    "user_action": "PAY_NOW"}}
    if returned:
        body["application_context"]["return_url"] = returned
    if cancelled:
        body["application_context"]["cancel_url"] = cancelled
    reply = _http("POST", "/v2/checkout/orders", body=body, token=token)
    order_id = str(reply.get("id") or "")
    if not order_id:
        raise AppError("PayPal created no order id",
                       code="PAYPAL_HTTP_ERROR", http_status=502)
    approve = next((str(link.get("href")) for link in reply.get("links") or []
                    if link.get("rel") in ("approve", "payer-action")), "")
    row = ORDERS.add({
        "kind": "buy", "order_id": order_id, "status": "created",
        "wallet": address, "tor": quote_row["tor"],
        "micro_tor": quote_row["micro_tor"], "usd": quote_row["usd"],
        "price_usd": quote_row["price_usd"], "note": str(note or "")[:200],
        "approve_url": approve, "created_at": _now(),
        "created_unix": int(time.time()), "capture_id": None, "txid": None,
        "height": None, "settled_at": None, "problem": None,
        "environment": environment()})
    return {"order": row, "approve_url": approve,
            "how": ["open approve_url and pay", 
                    "PayPal returns to return_url with ?token=<order_id>",
                    "POST /api/paypal/buy/<order_id>/capture settles it"]}




def capture_buy(order_id: str) -> dict:
    """Capture the PayPal payment, then send the TOR from the treasury.

    Idempotent by order id: a second capture returns the first settlement rather
    than paying twice. If the capture succeeds and the transfer fails, the order
    becomes ``paid_undelivered`` - the buyer's money is recorded as owed, which is
    the only honest state a shop can be in at that moment.
    """
    require_configured()
    row = _order_row(order_id)
    if row.get("status") in ("settled", "paid_undelivered"):
        return {"order": row, "settled": row.get("status") == "settled",
                "already": True, "txid": row.get("txid"),
                "height": row.get("height"), "problem": row.get("problem")}

    token = access_token()
    reply = _http("POST", f"/v2/checkout/orders/{order_id}/capture",
                  body={}, token=token)
    outcome = str(reply.get("status") or "")
    if outcome != "COMPLETED":
        raise AppError(f"PayPal did not capture this order (status {outcome!r})",
                       code="PAYPAL_CAPTURE_FAILED", http_status=402,
                       details={"order_id": order_id, "status": outcome})
    units = (reply.get("purchase_units") or [{}])[0]
    captures = ((units.get("payments") or {}).get("captures") or [{}])
    capture = captures[0] if captures else {}
    capture_id = str(capture.get("id") or "")
    try:
        paid = float(((capture.get("amount") or {}).get("value")) or 0)
    except (TypeError, ValueError):
        paid = 0.0
    if paid + 0.005 < float(row.get("usd") or 0):
        ORDERS.update(row["id"], status="underpaid", capture_id=capture_id,
                      paid_usd=paid, problem=f"captured {paid:.2f} of {row['usd']}")
        raise AppError(
            f"PayPal captured {paid:.2f} USD but the order was "
            f"{float(row.get('usd') or 0):.2f} - the TOR was not sent",
            code="PAYPAL_UNDERPAID", http_status=402,
            details={"order_id": order_id, "capture_id": capture_id})

    paid_row = ORDERS.update(row["id"], status="paid", capture_id=capture_id,
                             paid_usd=paid, paid_at=_now())
    try:
        sent = ledger.send_micro(treasury_wallet(), str(paid_row["wallet"]),
                                 int(paid_row["micro_tor"]),
                                 note=f"paypal buy {order_id}")
    except Exception as problem:  # noqa: BLE001 - money is already taken
        detail = str(getattr(problem, "message", problem))[:300]
        ORDERS.update(row["id"], status="paid_undelivered", problem=detail)
        raise AppError(
            "PayPal took the money but the TOR could not be delivered - the "
            "order is marked paid_undelivered and no retry is hidden",
            code="PAYPAL_UNDELIVERED", http_status=502,
            details={"order_id": order_id, "capture_id": capture_id,
                     "problem": detail})
    settled = ORDERS.update(row["id"], status="settled", settled_at=_now(),
                            txid=sent.get("txid"), height=sent.get("height"))
    return {"order": settled, "settled": True, "txid": sent.get("txid"),
            "height": sent.get("height"), "capture_id": capture_id}


# --------------------------------------------------------------------- selling
def start_sell(tor: float, wallet_name: str, *, note: str = "") -> dict:
    """Start a SELL: an order that names the exact deposit the treasury expects.

    Nothing is paid out here. The seller sends TOR on chain first, carrying the
    note this order returns, so the deposit can be proved before any fiat moves.
    """
    require_configured()
    name = str(wallet_name or "").strip()
    if not name:
        raise ValidationError("wallet_name is required to sell TOR")
    quote_row = quote(tor)
    row = ORDERS.add({"kind": "sell", "order_id": "", "status": "awaiting_tor",
                      "wallet": name, "tor": quote_row["tor"],
                      "micro_tor": quote_row["micro_tor"],
                      "usd": quote_row["usd"], "price_usd": quote_row["price_usd"],
                      "note": str(note or "")[:200],
                      "deposit_address": treasury_wallet(),
                      "created_at": _now(), "created_unix": int(time.time()),
                      "txid": None, "payout_at": None, "problem": None,
                      "environment": environment()})
    deposit_note = f"paypal-sell:{row['id']}"
    return {"order": {**row, "deposit_note": deposit_note},
            "how": [f"send {quote_row['amount_tor']} TOR from {name} to "
                    f"{treasury_wallet()}",
                    f"with the note {deposit_note!r} - it is the receipt",
                    "POST /api/paypal/sell/<id>/settle {\"txid\": ...} as admin "
                    "once the deposit has confirmed"]}


def settle_sell(order_ref: str, txid: str) -> dict:
    """Mark a sell's payout due - only against a deposit found on chain."""
    require_configured()
    row = ORDERS.get(order_ref)
    if row.get("kind") != "sell":
        raise ValidationError(f"{order_ref} is not a sell order")
    if row.get("status") == "payout_due":
        return {"order": row, "already": True, "payout_due": True}
    wanted = str(txid or "").strip()
    if not wanted:
        raise ValidationError("txid is required - the deposit is the receipt")
    history = ledger.history(treasury_wallet())
    records = history.get("records") if isinstance(history, dict) else None
    if not isinstance(records, list):
        raise AppError(
            "cannot read the treasury's history, so the deposit cannot be proved "
            "and the payout is not marked",
            code="PAYPAL_DEPOSIT_UNREADABLE", http_status=503)
    found = next((item for item in records
                  if isinstance(item, dict) and str(item.get("txid")) == wanted),
                 None)
    if found is None:
        raise ValidationError(
            f"transaction {wanted} is not in the treasury's history",
            details={"treasury": treasury_wallet()})
    sender = str(found.get("from") or "")
    amount = int(found.get("amount") or 0)
    if amount != int(row.get("micro_tor") or 0):
        raise ValidationError(
            "that transaction does not pay this order - the amount differs",
            details={"txid": wanted, "amount": amount,
                     "expected": row.get("micro_tor")})
    updated = ORDERS.update(row["id"], status="payout_due", txid=wanted,
                            paid_from=sender, payout_at=_now())
    return {"order": updated, "payout_due": True,
            "note": ("pay this seller from PayPal; this node does not move fiat "
                     "out on its own")}


# ------------------------------------------------------------------- webhooks
def verify_webhook(headers, raw_body: bytes | str) -> dict:
    """PayPal's own signature check - a webhook is data until it passes."""
    webhook_id = _env("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        raise AppError("webhook verification is not configured "
                       "(PAYPAL_WEBHOOK_ID) - the event is refused, not trusted",
                       code="PAYPAL_WEBHOOK_UNCONFIGURED", http_status=503)

    def held(name: str) -> str:
        for candidate in (name, name.lower(), name.replace("-", "_")):
            try:
                value = headers.get(candidate)
            except AttributeError:
                value = None
            if value:
                return str(value).strip()
        return ""

    event = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, bytes)
                       else raw_body or "{}")
    reply = _http("POST", "/v1/notifications/verify-webhook-signature", body={
        "auth_algo": held("paypal-auth-algo"),
        "cert_url": held("paypal-cert-url"),
        "transmission_id": held("paypal-transmission-id"),
        "transmission_sig": held("paypal-transmission-sig"),
        "transmission_time": held("paypal-transmission-time"),
        "webhook_id": webhook_id, "webhook_event": event}, token=access_token())
    if str(reply.get("verification_status") or "") != "SUCCESS":
        raise AppError("that webhook did not verify - refused",
                       code="PAYPAL_WEBHOOK_INVALID", http_status=401,
                       details={"status": reply.get("verification_status")})
    return {"event": event, "verified": True,
            "type": event.get("event_type") if isinstance(event, dict) else None}


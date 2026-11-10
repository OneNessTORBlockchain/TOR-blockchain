"""Wallet + payment endpoints (real signatures, real settlement).

Reading is public, spending is not: ``GET /api/wallets/<name>`` and
``GET /api/wallets/<name>/balance`` answer anyone, because balances are
permanent public ledger data (the full public balance sheet lives at
``/api/public/balances``). Sending, signing, the receive panel and the
transaction history still need a wallet session earned by the account's username/password
login — or by signing a challenge with the wallet's own Ed25519 key (see
``oneness_tor.services.wallet_access``). The operator's wallet list
(``GET /api/wallets``) stays admin-only because it exposes keystore metadata.

Public pages never ask for a token: a visitor signs in at ``/wallet`` and every
write below then works with that one wallet.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import ValidationError
from oneness_tor.services import ledger, payments, wallet_access
from oneness_tor.services.auth import is_admin_request, require_admin

bp = Blueprint("wallet_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return payload


def _amount(payload: dict) -> float:
    value = payload.get("amount", payload.get("amount_tor"))
    if value in (None, ""):
        raise ValidationError("amount (TOR) is required")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("amount must be a number, e.g. 1.5")


# ----------------------------------------------------------------- wallets
@bp.get("/wallets")
def wallets():
    """Every wallet on this node — admin only: it exposes keystore metadata."""
    require_admin(request)
    rows = ledger.all_wallets()
    return envelope({"wallets": rows, "count": len(rows),
                     "note": ("admin view: each wallet sees only its own balance "
                              "through a signed wallet session")})


@bp.post("/wallets")
def create_wallet():
    require_admin(request)
    payload = _body()
    if not payload.get("name"):
        raise ValidationError("name is required")
    return envelope(ledger.new_wallet(str(payload["name"]),
                                      int(payload.get("words") or 24)))


@bp.post("/wallets/<name>/challenge")
def wallet_challenge(name: str):
    """Public: the exact message this wallet must sign (5-minute nonce)."""
    return envelope(wallet_access.challenge(name))


@bp.post("/wallets/<name>/session")
def wallet_session(name: str):
    """Public: verify the signature and mint a short-lived balance session."""
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(wallet_access.verify(name, payload.get("nonce"),
                                        payload.get("signature")))


@bp.get("/wallets/<name>")
def get_wallet(name: str):
    """Public: the wallet's name, address, balances — keystore refs stay admin."""
    view = ledger.public_wallet(name)
    if not is_admin_request(request):
        view.pop("key_file", None)
        view.pop("has_mnemonic", None)
    return envelope(view)


@bp.get("/wallets/<name>/balance")
def wallet_balance(name: str):
    """Public: live balance from the node — no session, no admin token."""
    return envelope(ledger.balance_of(name))


@bp.get("/wallets/<name>/receive")
def wallet_receive(name: str):
    """Address, public key and real QR codes — owner session or admin."""
    wallet_access.authorize(request, name)
    return envelope(ledger.receive(name))


@bp.get("/wallets/<name>/history")
def wallet_history(name: str):
    """This wallet's whole transaction history, hex included (newest first)."""
    wallet_access.authorize(request, name)
    limit = request.args.get("limit", 2000, type=int) or 2000
    return envelope(ledger.history(name, limit=limit))


@bp.post("/wallets/<name>/send")
def wallet_send(name: str):
    """Owner (wallet session) or admin: sign + broadcast from ``name``.

    ``privacy`` is the wallet's own per-transaction answer —
    ``"public"`` (default) writes this wallet's address to the record,
    ``"anonymous"`` writes ``Tor-Anonymous-onion`` instead. A misspelt value is
    a 400 *before* anything is signed (see ``oneness_tor.services.privacy``).
    """
    wallet_access.authorize_wallet(request, name)
    payload = _body()
    to = str(payload.get("to") or "").strip()
    if not to:
        raise ValidationError("to is required")
    privacy = payload.get("privacy")
    token = payload.get("token")
    if token:
        amount_token = payload.get("amount_token", payload.get("amount"))
        return envelope(ledger.send(name, to,
                                    amount_token=float(amount_token or 0),
                                    token=str(token), note=payload.get("note", ""),
                                    privacy=privacy))
    return envelope(ledger.send(name, to, amount_tor=_amount(payload),
                                note=payload.get("note", ""), privacy=privacy))


@bp.post("/wallets/<name>/sign")
def wallet_sign(name: str):
    wallet_access.authorize_wallet(request, name)
    payload = _body()
    return envelope(ledger.sign_only(
        name, kind=str(payload.get("kind") or "transfer"),
        to=str(payload.get("to") or ""), amount=_amount(payload),
        token=payload.get("token"), note=payload.get("note", ""),
        privacy=payload.get("privacy")))


# ---------------------------------------------------------------- payments
@bp.get("/payments/plans")
def plans():
    return envelope({"plans": payments.plans()})


@bp.get("/payments/quote")
def quote():
    code = request.args.get("code")
    if not code:
        raise ValidationError("code is required")
    quantity = request.args.get("quantity", 1, type=float) or 1
    return envelope(payments.quote(code, quantity))


@bp.post("/payments/invoices")
def create_invoice():
    require_admin(request)
    payload = _body()
    invoice = payments.create_invoice(
        code=payload.get("code"),
        amount_tor=(float(payload["amount_tor"])
                    if payload.get("amount_tor") not in (None, "") else None),
        wallet=payload.get("wallet"), note=payload.get("note", ""),
        quantity=float(payload.get("quantity") or 1))
    return envelope({"invoice": invoice})


@bp.get("/payments/invoices")
def list_invoices():
    limit = request.args.get("limit", 25, type=int) or 25
    status = request.args.get("status")
    return envelope(payments.list_invoices(limit, status))


@bp.get("/payments/invoices/<invoice_id>")
def get_invoice(invoice_id: str):
    return envelope({"invoice": payments.get_invoice(invoice_id)})


@bp.post("/payments/invoices/<invoice_id>/pay")
def pay_invoice(invoice_id: str):
    """Settle an invoice from the visitor's own wallet (an admin may choose one)."""
    payload = _body()
    held = wallet_access.session(request)
    wallet = str(payload.get("wallet") or (held or {}).get("wallet") or "").strip()
    if not wallet:
        raise ValidationError(
            "wallet is required — sign in at /wallet first",
            details={"header": wallet_access.HEADER})
    wallet_access.authorize_wallet(request, wallet)
    return envelope(payments.pay_invoice(invoice_id, wallet))


@bp.get("/payments/revenue")
def revenue():
    return envelope(payments.revenue())

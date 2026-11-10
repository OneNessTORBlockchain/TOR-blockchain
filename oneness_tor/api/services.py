"""Smart-contract registry and Tor daemon control endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import pricing
from core.errors import FeeError, Forbidden, ValidationError
from oneness_tor.services import (contracts, ledger, paypal_gate, tor_service,
                                  wallet_access)
from oneness_tor.services.auth import is_admin_request, require_admin

bp = Blueprint("services_api", __name__, url_prefix="/api")


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


def _ensure_funds(wallet: str, code: str, quantity: float = 1) -> None:
    """Fail before generation when the payer cannot cover the LOCKED price."""
    price_micro = pricing.micro(code, quantity)
    account = ledger.balance_of(wallet)
    balance = int(account.get("balance_micro") or 0)
    if balance < price_micro:
        raise FeeError(
            f"{wallet} holds {account.get('balance_tor') or '0'} TOR but {code} costs "
            f"{pricing.price(code) * quantity:g} TOR",
            details={"wallet": wallet, "balance_micro": balance,
                     "required_micro": price_micro, "code": code})
    if account.get("warning"):
        raise FeeError(f"cannot read the balance of {wallet}: {account['warning']}")


def _payer(payload: dict) -> str:
    """The wallet that owns/pays for this call: the visitor's session wallet or admin choice."""
    held = wallet_access.session(request)
    wallet = str(payload.get("wallet") or (held or {}).get("wallet") or "")
    if not wallet and is_admin_request(request):
        wallet = "founder"
    if not wallet:
        raise Forbidden(
            "sign in at /wallet first — this action is paid and owned by your own "
            "account wallet", details={"header": wallet_access.HEADER})
    wallet_access.authorize_wallet(request, wallet)
    return wallet


def _viewer() -> tuple[str | None, bool]:
    """``(session wallet address, is admin)`` for owner-aware reads."""
    held = wallet_access.session(request)
    return (str(held.get("address") or "") if held else None,
            is_admin_request(request))


# --------------------------------------------------------------- contracts
@bp.get("/contracts/templates")
def contract_templates():
    return envelope({"templates": contracts.templates()})


@bp.get("/contracts")
def contract_list():
    limit = request.args.get("limit", 50, type=int) or 50
    address, admin = _viewer()
    return envelope(contracts.list_contracts(limit, viewer_address=address,
                                             admin=admin))


@bp.get("/contracts/mine")
def contract_mine():
    held = wallet_access.session(request)
    if not held:
        raise ValidationError(
            "wallet session required — POST /api/auth/login first",
            details={"header": wallet_access.HEADER})
    limit = request.args.get("limit", 50, type=int) or 50
    return envelope(contracts.list_mine(str(held.get("address") or ""), limit))


@bp.get("/contracts/nodes")
def contract_nodes():
    return envelope({"nodes": contracts.authorized_nodes()})


@bp.post("/contracts")
def contract_create():
    payload = _body()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValidationError("name is required")
    wallet = _payer(payload)
    _ensure_funds(wallet, "contract_create")
    owner_wallet = str(payload.get("owner_wallet") or wallet)
    wallet_access.authorize_wallet(request, owner_wallet)
    return envelope(contracts.deploy(
        name=name, template=payload.get("template"), source=payload.get("source"),
        owner_wallet=owner_wallet, wallet=wallet))


@bp.get("/contracts/<contract_id>")
def contract_get(contract_id: str):
    address, admin = _viewer()
    return envelope({"contract": contracts.get_contract(
        contract_id, viewer_address=address, admin=admin)})


@bp.post("/contracts/<contract_id>/call")
def contract_call(contract_id: str):
    payload = _body()
    function = str(payload.get("function") or "").strip()
    if not function:
        raise ValidationError("function is required")
    address, admin = _viewer()
    contracts.require_owner(contract_id, viewer_address=address, admin=admin)
    return envelope(contracts.call(contract_id, function, payload.get("args"),
                                   actor_address=address, admin=admin))


# --------------------------------------------------------------------- tor
@bp.get("/tor/status")
def tor_status():
    return envelope(tor_service.status())


@bp.post("/tor/new-identity")
def tor_new_identity():
    require_admin(request)
    return envelope(tor_service.new_identity())


@bp.get("/tor/node")
def tor_node():
    """The onion link this node itself is published at (never invented)."""
    return envelope(tor_service.node_status())


@bp.post("/tor/node")
def tor_node_publish():
    """ADD_ONION for the whole API: {port, refresh} (admin)."""
    require_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(tor_service.publish_node(payload.get("port"),
                                             refresh=bool(payload.get("refresh"))))


@bp.delete("/tor/node")
def tor_node_unpublish():
    """DEL_ONION the service that publishes this API (admin)."""
    require_admin(request)
    return envelope(tor_service.unpublish_node())


@bp.post("/tor/start")
def tor_start():
    """Start Tor on this host when the control port is closed (admin, tor.auto_start)."""
    require_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(tor_service.ensure_daemon(force=bool(payload.get("force"))))


@bp.get("/tor/key")
def tor_key():
    """This node's onion private key, so it can be persisted in the environment.

    Admin only, never cached, never logged: a Render container's disk is wiped on
    every deploy, and this is the one value that keeps the same .onion address
    across redeploys (``ONENESS_TOR_ONION_KEY``).
    """
    require_admin(request)
    response = envelope({"onion": tor_service.node_url() or None,
                         "env": "ONENESS_TOR_ONION_KEY",
                         "key": tor_service.onion_key_env()})
    response.headers["Cache-Control"] = "no-store, no-cache, private"
    return response


# ------------------------------------------------------------- the PayPal gate
# Registered on this blueprint rather than a new one on purpose: no route here
# needs `app.py` to change, so the gate cannot be half-deployed beside whatever
# else is being edited in that file.
@bp.get("/paypal/status")
def paypal_status():
    """Whether the gate is open, at what price - and never a credential."""
    return envelope(paypal_gate.status())


@bp.get("/paypal/quote")
def paypal_quote():
    """Price a quantity of TOR in USD at the platform's own anchor."""
    return envelope(paypal_gate.quote(request.args.get("tor") or 0))


@bp.post("/paypal/buy")
def paypal_buy():
    """Open a BUY: a real PayPal order for the wallet address you name."""
    payload = _body()
    return envelope(paypal_gate.create_buy(
        payload.get("tor"), str(payload.get("wallet") or ""),
        note=str(payload.get("note") or "")))


@bp.post("/paypal/buy/<order_id>/capture")
def paypal_capture(order_id: str):
    """Capture the money, then deliver the TOR - idempotent by order id."""
    return envelope(paypal_gate.capture_buy(order_id))


@bp.post("/paypal/sell")
def paypal_sell():
    """Open a SELL from a wallet this session owns (fiat out for TOR in)."""
    held = wallet_access.session(request)
    if not held:
        raise Forbidden(
            "sign in at /wallet first - a sell pays fiat out for TOR you own",
            details={"header": wallet_access.HEADER})
    payload = _body()
    return envelope(paypal_gate.start_sell(
        payload.get("tor"), str(payload.get("wallet") or held.get("wallet") or ""),
        note=str(payload.get("note") or "")))


@bp.post("/paypal/sell/<order_ref>/settle")
def paypal_settle(order_ref: str):
    """Admin: prove the TOR deposit on chain, then mark the payout due."""
    require_admin(request)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return envelope(paypal_gate.settle_sell(order_ref,
                                            str(payload.get("txid") or "")))


@bp.post("/paypal/webhook")
def paypal_webhook():
    """PayPal's event door: verified with PayPal itself, never trusted on arrival."""
    return envelope(paypal_gate.verify_webhook(request.headers,
                                               request.get_data() or b""))


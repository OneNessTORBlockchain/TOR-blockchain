"""The membership seam the three paid ANONYMOUS products call (no /api prefix).

ANONYMOUS Desktop, the ANONYMOUS webapp and the ANONYMOUS API talk to the chain
through exactly these paths, so the names in ``WALLET-IDENTITY.md`` are the names
on the wire and nothing has to be rewritten on either side:

    POST /wallet/verify                      is this signature this wallet's?
    GET  /membership/<address>               membership() + the five free messages
    GET  /membership/<address>/receipts      every paid period, newest first
    POST /membership/<address>/use-free      spend one free message (402 at zero)
    POST /membership/<address>/invoices      freeze the TOR amount to pay
    POST /membership/<address>/settle        verify the transfer, then activate
    POST /membership/<address>/cancel        end the period now (no refund)

and, for the hosted webapp's per-visitor VMs, five more on the same seam:

    POST /membership/<address>/sessions                    open (or reuse) a VM lease
    GET  /membership/<address>/sessions                    this wallet's leases
    POST /membership/<address>/sessions/<id>/heartbeat      keep it alive
    POST /membership/<address>/sessions/<id>/close          give it back
    GET  /membership/<address>/verify-tx/<txid>            is this transfer a payment?

Reading is public -- balances, prices and subscriptions are public chain facts.
Everything that changes a wallet's record needs that wallet's own signature
(:mod:`oneness_tor.services.wallet_proof`), so a product can never promote
somebody else's wallet, replay an old call, or settle a payment it did not make.
The one thing that stays private is **who has a VM alive**: ``GET
/membership/<address>`` carries no session data at all, and a wallet's leases are
read through its own signature (:mod:`oneness_tor.services.wallet_sessions`).

The one honest limit of this file: ``/membership/<address>`` **reports**. It
does not start, stop or block a VM, a message or a key. The products decide what
"inactive" means for them -- the sessions only make sure the chain knows which
VM belongs to which wallet, so a product cannot hand out a second one.
"""
from __future__ import annotations

import base58
from flask import Blueprint, jsonify, request

from core.errors import ValidationError
from oneness_tor.services import membership, wallet_proof, wallet_sessions
from oneness_tor.services.wallet_access import address_for_pubkey

bp = Blueprint("membership_api", __name__)


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _body() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


# ------------------------------------------------------- who is this wallet?
@bp.post("/wallet/verify")
def wallet_verify():
    """Public and stateless: does ``signature`` belong to ``pubkey``?

    A product can use this for a second opinion from the chain itself; the
    products also verify locally, so a stolen RPC can never be the only thing
    standing between a wallet and its subscription.
    """
    payload = _body()
    pubkey = str(payload.get("pubkey") or payload.get("pubkey_base58") or "")
    message = payload.get("message")
    signature = str(payload.get("signature") or "")
    if not pubkey or message in (None, "") or not signature:
        raise ValidationError("pubkey, message and signature are required",
                              details={"standard": wallet_proof.STANDARD})
    text = message if isinstance(message, str) else str(message)
    valid = wallet_proof.verify_signature(pubkey, text, signature)
    answer = {"standard": wallet_proof.STANDARD, "valid": valid,
              "pubkey_base58": pubkey, "message": text,
              "algorithm": "ed25519",
              "how": ("sign the canonical string: method\\npath\\ntimestamp\\n"
                      "nonce\\nsha256(raw body) hex, then send it back as "
                      + ", ".join(wallet_proof.HEADERS))}
    if valid:
        answer["address"] = address_for_pubkey(base58.b58decode(pubkey))
    return envelope(answer)


@bp.get("/membership/<address>")
def membership_identity(address: str):
    """The whole answer a paid product needs: active, until, plan, free left."""
    return envelope(membership.identity(
        address, product=str(request.args.get("product") or "")))


@bp.get("/membership/<address>/receipts")
def membership_receipts(address: str):
    limit = request.args.get("limit", 50, type=int) or 50
    return envelope(membership.receipts(address, limit))


# --------------------------------------------------- the five free messages
@bp.post("/membership/<address>/use-free")
def membership_use_free(address: str):
    """Spend one of the wallet's five free messages. 402 when they are gone."""
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    spent = membership.use_free(
        proved["address"], cost=int(payload.get("cost") or 1),
        product=str(payload.get("product") or ""),
        note=str(payload.get("note") or ""),
        session=str(payload.get("session") or ""))
    return envelope({**spent, "proof": {"address": proved["address"],
                                        "nonce": proved["nonce"]}})


# ------------------------------------------------------------- the money
@bp.post("/membership/<address>/invoices")
def membership_subscribe(address: str):
    """Open the invoice for this wallet: the TOR amount is frozen here."""
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    return envelope(membership.subscribe(
        proved["address"], months=float(payload.get("months") or 1),
        privacy=payload.get("privacy"),
        note=str(payload.get("note") or ""),
        product=str(payload.get("product") or "")))


@bp.post("/membership/<address>/settle")
def membership_settle(address: str):
    """Verify the transfer on chain, then record the period in the contract."""
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    invoice_id = str(payload.get("invoice_id") or payload.get("invoice") or "")
    if not invoice_id:
        raise ValidationError("invoice_id is required")
    return envelope(membership.settle(proved["address"], invoice_id,
                                      str(payload.get("txid") or "")))


@bp.post("/membership/<address>/cancel")
def membership_cancel(address: str):
    """End the period now -- the contract's own rule, no invented refund.

    Ending the period also **releases this wallet's VM leases** (`sessions_closed`
    in the answer), because the thing that paid for them is gone. Stopping the
    containers is still the product's job: the chain reports, it does not kill.
    """
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    ended = membership.cancel(proved["address"],
                              reason=str(payload.get("reason") or ""))
    freed = wallet_sessions.close_all(
        proved["address"], reason="membership cancelled")
    return envelope({**ended, "sessions_closed": freed["closed"],
                     "sessions": freed["sessions"]})


# ------------------------------------------- the VM lease behind each wallet
@bp.post("/membership/<address>/sessions")
def membership_session_open(address: str):
    """Open (or hand back) this wallet's VM lease in one product.

    ``tenant`` is the VM's name; it defaults to ``"<product>-<session id>"``. A
    wallet that already holds a live lease in this product gets the **same** one
    (``reused: true``) -- that is how one visitor gets one VM. ``replace: true``
    retires the old lease, and its id is a 409 from then on.
    """
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    return envelope(wallet_sessions.open_(
        proved["address"], str(payload.get("product") or ""),
        tenant=str(payload.get("tenant") or ""),
        ttl=payload.get("ttl") or 0,
        replace=bool(payload.get("replace")),
        note=str(payload.get("note") or "")))


@bp.get("/membership/<address>/sessions")
def membership_sessions(address: str):
    """This wallet's leases -- read through its own signature, never publicly.

    Which wallet has a VM alive right now is not a chain fact to publish, so this
    is the one read on the seam that needs the four headers.
    """
    proved = wallet_proof.require(request, address=address)
    return envelope(wallet_sessions.listing(
        proved["address"], product=str(request.args.get("product") or "")))


@bp.post("/membership/<address>/sessions/<session_id>/heartbeat")
def membership_session_heartbeat(address: str, session_id: str):
    """Renew a lease and say whether the wallet may still be served (409 if dead)."""
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    return envelope(wallet_sessions.heartbeat(proved["address"], session_id,
                                              ttl=payload.get("ttl") or 0))


@bp.post("/membership/<address>/sessions/<session_id>/close")
def membership_session_close(address: str, session_id: str):
    """Give one VM lease back now (idempotent)."""
    proved = wallet_proof.require(request, address=address)
    payload = _body()
    return envelope(wallet_sessions.close(proved["address"], session_id,
                                          reason=str(payload.get("reason") or "")))


# ------------------------------------------------- is this transfer a payment?
@bp.get("/membership/<address>/verify-tx/<txid>")
def membership_verify_tx(address: str, txid: str):
    """Read-only: would this transfer settle this wallet's membership?

    Public, because a ledger record already is. It **activates nothing** -- the
    period still starts only at ``/settle`` -- and it asks the same function
    ``settle`` uses, so it can never promise what settlement would refuse.
    """
    return envelope(membership.check_payment(
        address, txid, invoice_id=str(request.args.get("invoice_id") or "")))

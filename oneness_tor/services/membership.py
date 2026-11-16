"""Paid membership: the wallet payment, and the contract that records it.

One plan, one price, one honest settlement:

* **The price is the plan's** -- ``membership.usd`` (``ONENESS_MEMBERSHIP_USD``),
  $11.99 per ``membership.days`` (30) days. It is never hardcoded in a page.
* **The TOR amount is never invented** -- it is the USD price divided by the
  price engine's live TOR/USD rate (``market_engine``), rounded to the nearest
  microTOR (``MICRO_PER_TOR`` = 10**8). A quote is therefore reproducible from
  two numbers the node published.
* **The payment is a real transaction** -- ``ledger.send_micro`` signs the
  canonical message with the payer's own Ed25519 key and broadcasts it, so the
  membership fee appears in ``/api/public/records`` exactly like any transfer.
  The payer may send it **anonymously**: the public record then reads
  ``Tor-Anonymous-onion`` instead of the address (``services.privacy``).
* **The subscription lives in a program** -- ``activate`` calls the deployed
  ``membership`` contract (``services.contracts``) with the settled
  ``amount_micro`` and the ``txid``; the contract stores one account per wallet
  with ``payments``, ``paid_micro`` and ``expires_at``. ``expires_at`` is the
  only value a product reads.

Nothing here gates anything: ``status()`` reports the fact of a subscription and
stops there. A product that wants to ask for money asks; this module never
blocks a wallet, a message or a VM.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from core.config import settings
from core.errors import (AppError, ChainUnavailable, Forbidden, MembershipRequired,
                         NotFound, ValidationError)
from core.node_client import node
from core.validator import MICRO_PER_TOR, format_tor
from oneness_tor.services import contracts, ledger
from oneness_tor.services.store import Collection, data_dir, read_json, write_json

#: The document/answer id every JSON payload below carries.
STANDARD = "oneness-tor-membership/1"
#: Published fallbacks -- the live values come from ``config.yaml`` / the env.
DEFAULT_USD = 11.99
DEFAULT_DAYS = 30
#: micro-USD per USD: the rate is carried as an integer so no float can drift.
MICRO_USD = 1_000_000
#: Environment name the four products already reserve for the contract id.
PRODUCT_CONTRACT_ENV = "PENTEST_MEMBERSHIP_CONTRACT"

MEMBERS = Collection(lambda: data_dir("membership") / "members.json", "mem")
INVOICES = Collection(lambda: data_dir("membership") / "invoices.json", "sub")


# --------------------------------------------------------------- the plan
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(stamp: int | float) -> str:
    """Unix seconds as ``2026-12-03T00:00:00+00:00`` (the products' ``until``)."""
    return datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(
        timespec="seconds")


def _plan_id() -> str:
    return str(settings.get("membership.plan_id") or "oneness-membership")


def _label() -> str:
    return str(settings.get("membership.plan_label")
               or "OneNess TOR membership - 30 days")


def usd_price() -> float:
    """The published USD price of one period (yours to set, never a guess)."""
    try:
        value = float(settings.get("membership.usd") or DEFAULT_USD)
    except (TypeError, ValueError):
        raise ValidationError(
            "membership.usd must be a number",
            details={"received": str(settings.get("membership.usd"))})
    if value <= 0:
        raise ValidationError("membership.usd must be greater than zero")
    return value


def period_days() -> int:
    """Days one payment buys (``membership.days``, default 30)."""
    try:
        value = int(settings.get("membership.days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        raise ValidationError("membership.days must be a whole number of days")
    if value <= 0:
        raise ValidationError("membership.days must be greater than zero")
    return value


def treasury() -> str:
    """Where the money goes: the plan's treasury, else the node's fee receiver."""
    configured = str(settings.get("membership.treasury") or "").strip()
    if configured:
        return configured
    try:
        return str(node.fee_receiver())
    except Exception as problem:  # noqa: BLE001 - the reason is what matters
        raise ChainUnavailable(
            "no membership treasury is configured and the node's fee receiver "
            "is unreadable - set membership.treasury or "
            "ONENESS_MEMBERSHIP_TREASURY",
            details={"cause": str(getattr(problem, "message", problem))[:200]})


def tor_usd() -> float:
    """TOR in USD, straight from the price engine (anchor when it is silent)."""
    from oneness_tor.services import market_engine

    price = 0.0
    try:
        price = float((market_engine.snapshot().get("tor") or {}).get("price_usd")
                      or 0.0)
    except Exception:  # noqa: BLE001 - a broken feed falls back to the anchor
        price = 0.0
    if price <= 0:
        price = float(market_engine.anchor_usd() or 0.0)
    if price <= 0:
        raise ValidationError("the TOR/USD rate is unreadable - no amount can be "
                              "quoted honestly")
    return price


def micro_for(usd: float, rate: float) -> int:
    """USD -> microTOR at ``rate`` (USD per TOR), rounded to the nearest micro.

    Integer arithmetic only, so the same two numbers always give the same
    amount: ``(cents * 10 000 * 10**8) / (rate * 10**6)`` = microTOR.
    """
    if float(rate) <= 0:
        raise ValidationError("the TOR/USD rate must be greater than zero",
                              details={"tor_usd": rate})
    cents = int(round(float(usd) * 100))
    if cents <= 0:
        raise ValidationError("the membership price must be greater than zero",
                              details={"usd": usd})
    numerator = cents * 10_000 * MICRO_PER_TOR
    denominator = int(round(float(rate) * MICRO_USD))
    return int((numerator + denominator // 2) // denominator)


def plan() -> dict:
    """The product the visitor is buying, with the treasury already resolved."""
    return {"standard": STANDARD, "id": _plan_id(), "label": _label(),
            "usd": usd_price(), "days": period_days(),
            "period": f"{period_days()} days", "currency": "USD",
            "settled_in": "TOR", "treasury": treasury(),
            "contract": contract_id()}


def quote(months: float = 1) -> dict:
    """$11.99 (or ``months`` of it) expressed in TOR at the live rate."""
    months = 1.0 if months is None else float(months)
    if months <= 0:
        raise ValidationError("months must be greater than zero",
                              details={"months": months})
    rate = tor_usd()
    usd = round(usd_price() * months, 2)
    days = int(round(period_days() * months))
    micro = micro_for(usd, rate)
    return {"standard": STANDARD, "plan": _plan_id(), "label": _label(),
            "months": months, "days": days, "usd": usd,
            "usd_cents": int(round(usd * 100)), "tor_usd": rate,
            "micro": micro, "tor": round(micro / MICRO_PER_TOR, 8),
            "amount_tor": format_tor(micro),
            "note": ("the USD price is fixed; TOR is converted at the price "
                     "engine's rate for this quote")}


# ------------------------------------------------------------ the contract
def contract_path():
    return data_dir("membership") / "contract.json"


def contract_id() -> str:
    """The membership contract id: config/env first, then this node's deploy."""
    configured = str(settings.get("membership.contract") or "").strip()
    if configured:
        return configured
    from_env = str(os.environ.get(PRODUCT_CONTRACT_ENV) or "").strip()
    if from_env:
        return from_env
    stored = read_json(contract_path(), {}) or {}
    return str((stored or {}).get("contract_id") or "").strip()


def contract() -> dict:
    """The deployed program: identity, plan and its accounts - no secrets."""
    identifier = contract_id()
    if not identifier:
        return {"deployed": False, "contract_id": "", "template": "membership",
                "note": ("no membership contract is deployed on this node - "
                         "POST /api/payments/membership/contract (admin) deploys "
                         "the program for 5 TOR")}
    try:
        row = contracts.get_contract(identifier, admin=True)
    except NotFound:
        return {"deployed": False, "contract_id": identifier,
                "template": "membership",
                "note": "the configured contract id is not in this node's registry"}
    state = dict(row.get("state") or {})
    accounts = dict(state.get("accounts") or {})
    collected = int(state.get("collected_micro") or 0)
    return {"deployed": True, "contract_id": identifier,
            "name": row.get("name"), "template": row.get("template"),
            "status": row.get("status"), "owner_address": row.get("owner_address"),
            "functions": row.get("functions") or [],
            "source_sha3": row.get("source_sha3"),
            "compile": row.get("compile"), "created_at": row.get("created_at"),
            "txid": row.get("txid"),
            "plan": state.get("plan"), "days": state.get("days"),
            "price_usd_cents": state.get("price_usd_cents"),
            "treasury": state.get("treasury"),
            "accounts": len(accounts), "collected_micro": collected,
            "collected_tor": format_tor(collected),
            "calls": int(state.get("calls") or 0)}


def deploy_contract(*, name: str = "OneNess TOR membership",
                    owner_wallet: str = "founder", wallet: str = "founder") -> dict:
    """Deploy the ``membership`` program (5 TOR, compile-checked) and remember it."""
    priced = quote(1)
    pay_to = treasury()
    deployed = contracts.deploy(
        name=name, template="membership", owner_wallet=owner_wallet, wallet=wallet,
        state={"plan": priced["plan"], "label": priced["label"],
               "price_usd_cents": priced["usd_cents"], "days": priced["days"],
               "treasury": pay_to,
               "tor_usd_micro": int(round(priced["tor_usd"] * MICRO_USD))})
    identifier = str(deployed["contract"]["id"])
    write_json(contract_path(), {"contract_id": identifier, "name": name,
                                 "plan": priced["plan"], "treasury": pay_to,
                                 "price_usd_cents": priced["usd_cents"],
                                 "days": priced["days"],
                                 "txid": deployed.get("txid"),
                                 "deployed_at": _now(),
                                 "deployed_unix": int(time.time())})
    return {"contract": deployed["contract"], "txid": deployed.get("txid"),
            "charge": deployed.get("charge"), "plan": priced,
            "treasury": pay_to,
            "authorized_nodes": deployed.get("authorized_nodes") or []}


# --------------------------------------------------------------- the money
def overview(months: float = 1) -> dict:
    """Everything a pay page needs in one call: plan, quote, contract, treasury."""
    priced = quote(months)
    listed = plan()
    return {"standard": STANDARD, "plan": listed, "quote": priced,
            "contract": contract(),
            "treasury": priced.get("treasury") or listed["treasury"],
            "privacy": {"public": "the payer's address is written on chain",
                        "anonymous": ("Tor-Anonymous-onion is written instead - the "
                                      "buyer's own receipt keeps the real address")}}


def create_invoice(*, wallet: str = "", address: str = "", months: float = 1,
                   note: str = "", privacy: object = None) -> dict:
    """Freeze one TOR amount for one wallet. No money moves yet."""
    priced = quote(months)
    pay_to = treasury()
    invoice = INVOICES.add({
        "plan": priced["plan"], "label": priced["label"],
        "months": priced["months"], "days": priced["days"],
        "usd": priced["usd"], "tor_usd": priced["tor_usd"],
        "micro": priced["micro"], "amount_tor": priced["amount_tor"],
        "treasury": pay_to, "contract": contract_id(), "status": "open",
        "wallet": str(wallet or ""), "address": str(address or ""),
        "note": str(note or "")[:200],
        "privacy": privacy if isinstance(privacy, str) else "",
        "created_at": _now(), "created_unix": int(time.time()),
        "paid_at": None, "txid": None, "height": None, "expires_at": None})
    return {"standard": STANDARD, "invoice": invoice, "quote": priced,
            "treasury": pay_to, "pay_to": pay_to,
            "contract": contract_id()}


def get_invoice(invoice_id: str) -> dict:
    return INVOICES.get(invoice_id)


def _member_row(address: str) -> dict | None:
    wanted = str(address or "").strip()
    for row in MEMBERS.all():
        if str(row.get("address") or "") == wanted:
            return row
    return None


def _expires(existing: dict | None, now: int, days: int, function: str) -> int:
    base = now
    if existing and function == "renew":
        base = max(int(existing.get("expires_at") or 0), now)
    return base + int(days) * 86_400


def _receipt(invoice: dict, member: dict) -> dict:
    return {"standard": STANDARD, "invoice": invoice.get("id"),
            "plan": invoice.get("plan"), "label": invoice.get("label"),
            "days": invoice.get("days"), "usd": invoice.get("usd"),
            "amount_tor": invoice.get("amount_tor"),
            "micro": invoice.get("micro"), "treasury": invoice.get("treasury"),
            "txid": invoice.get("txid"), "height": invoice.get("height"),
            "paid_at": invoice.get("paid_at"), "address": invoice.get("address"),
            "expires_at": (member.get("account") or {}).get("expires_at"),
            "contract": (member.get("contract") or {}).get("contract"),
            "contract_ok": (member.get("contract") or {}).get("ok")}


def activate(address: str, *, days: int, amount_micro: int, txid: str = "",
             wallet: str = "", now: int | None = None) -> dict:
    """Book a settled payment on the account and let the contract record it.

    ``subscribe`` opens a period, ``renew`` extends the one still running so no
    paid time is ever lost. When no contract is deployed the account is still
    recorded -- the payment already landed on chain -- and the answer says so
    under ``contract.ok = None``.
    """
    address = str(address or "").strip()
    if not address:
        raise ValidationError("activate needs the paying wallet's address")
    moment = int(now if now is not None else time.time())
    days = int(days)
    if days <= 0:
        raise ValidationError("activate needs a positive number of days")
    existing = _member_row(address)
    function = ("renew" if existing
                and int(existing.get("expires_at") or 0) > moment else "subscribe")
    identifier = contract_id()
    record: dict = {"deployed": bool(identifier), "contract": identifier,
                    "function": function}
    if identifier:
        args = {"address": address, "amount_micro": int(amount_micro),
                "days": days, "txid": str(txid or ""), "now": moment,
                "tor_usd_micro": int(round(tor_usd() * MICRO_USD))}
        try:
            called = contracts.call(identifier, function, args=args, admin=True)
            row = dict((called.get("state") or {}).get("accounts") or {}).get(
                address) or {}
            expires = int(row.get("expires_at") or 0)
            record.update({"ok": True, "expires_at": expires,
                           "at": called.get("at"),
                           "collected_micro": int(
                               (called.get("state") or {}).get("collected_micro")
                               or 0)})
        except Exception as problem:  # noqa: BLE001 - the payment already landed
            expires = _expires(existing, moment, days, function)
            record.update({"ok": False,
                           "error": str(getattr(problem, "message", problem))[:300],
                           "expires_at": expires})
    else:
        expires = _expires(existing, moment, days, function)
        record.update({"ok": None, "expires_at": expires,
                       "note": ("no membership contract is deployed - the receipt "
                                "is recorded off-chain until one exists")})
    fields = {"address": address, "plan": _plan_id(), "wallet": str(wallet or ""),
              "status": "active" if expires > moment else "expired",
              "expires_at": int(expires), "days": days,
              "paid_micro": int((existing or {}).get("paid_micro") or 0)
              + int(amount_micro),
              "payments": int((existing or {}).get("payments") or 0) + 1,
              "last_txid": str(txid or ""), "last_paid_at": moment,
              "updated_at": _now(), "contract_call": record}
    if existing:
        account = MEMBERS.update(str(existing[MEMBERS.key]), **fields)
    else:
        account = MEMBERS.add({**fields, "created_at": _now(),
                               "created_unix": moment})
    return {"standard": STANDARD, "account": account, "contract": record,
            "function": function}


def pay(invoice_id: str, wallet: str, *, privacy: object = None) -> dict:
    """Settle an invoice with a real signed transfer, then record the period."""
    invoice = INVOICES.get(invoice_id)
    if invoice.get("status") == "paid":
        raise ValidationError(f"invoice {invoice_id} is already paid",
                              details={"txid": invoice.get("txid")})
    if invoice.get("status") != "open":
        raise ValidationError(f"invoice {invoice_id} is {invoice.get('status')}")
    held = ledger.load_wallet(str(wallet))
    address = str(held.get("address") or "")
    settlement = ledger.send_micro(
        str(wallet), str(invoice["treasury"]), int(invoice["micro"]),
        note=f"membership {invoice_id} {int(invoice['days'])}d",
        privacy=privacy)
    member = activate(address, days=int(invoice["days"]),
                      amount_micro=int(invoice["micro"]),
                      txid=str(settlement.get("txid") or ""),
                      wallet=str(wallet))
    updated = INVOICES.update(invoice_id, status="paid", paid_at=_now(),
                              txid=settlement.get("txid"),
                              height=settlement.get("height"), address=address,
                              privacy=str(settlement.get("privacy") or ""),
                              expires_at=(member.get("account") or {}).get(
                                  "expires_at"))
    return {"standard": STANDARD, "invoice": updated, "membership": member,
            "txid": settlement.get("txid"), "height": settlement.get("height"),
            "settlement": settlement, "receipt": _receipt(updated, member)}


# --------------------------------------------------------------- the report
def status(address: str, *, now: int | None = None) -> dict:
    """Is this wallet a member right now? A fact, not a gate."""
    moment = int(now if now is not None else time.time())
    address = str(address or "").strip()
    row = _member_row(address)
    if not row:
        return {"standard": STANDARD, "address": address, "member": False,
                "active": False, "plan": _plan_id(), "label": _label(),
                "expires_at": 0, "expires_in_seconds": 0, "days_left": 0,
                "payments": 0, "paid_micro": 0, "paid_tor": "0",
                "last_txid": None, "wallet": "", "updated_at": None,
                "contract": contract_id(), "renew_at_usd": usd_price()}
    expires = int(row.get("expires_at") or 0)
    remaining = max(0, expires - moment)
    paid = int(row.get("paid_micro") or 0)
    return {"standard": STANDARD, "address": address, "member": True,
            "active": expires > moment, "plan": row.get("plan") or _plan_id(),
            "label": _label(), "expires_at": expires,
            "expires_in_seconds": remaining,
            "days_left": round(remaining / 86_400, 2),
            "payments": int(row.get("payments") or 0), "paid_micro": paid,
            "paid_tor": format_tor(paid), "last_txid": row.get("last_txid") or None,
            "wallet": row.get("wallet") or "", "updated_at": row.get("updated_at"),
            "contract": contract_id(), "renew_at_usd": usd_price()}


def receipts(address: str, limit: int = 50) -> dict:
    """Every paid membership invoice of one wallet, newest first."""
    wanted = str(address or "").strip()
    rows = [row for row in INVOICES.all()
            if str(row.get("address") or "") == wanted
            and row.get("status") == "paid"]
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    rows = rows[:max(1, int(limit))]
    total = sum(int(row.get("micro") or 0) for row in rows)
    return {"standard": STANDARD, "address": wanted, "receipts": rows,
            "count": len(rows),
            "totals": {"paid_micro": total, "paid_tor": format_tor(total)}}


# ------------------------------------------- the five free messages per wallet
#: The products' free tier: five **sent** messages per wallet, once, forever.
#: It is counted here (the chain) and not in a product, so uninstalling the
#: webapp, wiping the Desktop or starting a second browser does not refill it.
DEFAULT_FREE_MESSAGES = 5
FREE_LOCK = threading.RLock()


def free_path():
    return data_dir("membership") / "free.json"


def free_limit() -> int:
    """``membership.free_messages`` (``ONENESS_MEMBERSHIP_FREE``), default 5."""
    try:
        value = int(settings.get("membership.free_messages")
                    or DEFAULT_FREE_MESSAGES)
    except (TypeError, ValueError):
        return DEFAULT_FREE_MESSAGES
    return max(0, value)


def _free_book() -> dict:
    held = read_json(free_path(), {}) or {}
    wallets = held.get("wallets") if isinstance(held, dict) else None
    return dict(wallets) if isinstance(wallets, dict) else {}


def _write_free_book(wallets: dict) -> None:
    write_json(free_path(), {"standard": STANDARD, "wallets": wallets,
                             "limit": free_limit(), "updated_at": _now()})


def free_state(address: str) -> dict:
    """How many free messages this wallet has left (never a guess)."""
    wanted = str(address or "").strip()
    row = _free_book().get(wanted) or {}
    limit = free_limit()
    used = int(row.get("used") or 0)
    return {"standard": STANDARD, "address": wanted, "limit": limit,
            "used": used, "left": max(0, limit - used),
            "exhausted": used >= limit,
            "first_used_unix": row.get("first_used_unix"),
            "last_used_unix": row.get("last_used_unix"),
            "last_product": row.get("last_product") or "",
            "last_session": row.get("last_session") or "",
            "turns": list(row.get("turns") or [])}


def use_free(address: str, *, cost: int = 1, product: str = "",
             note: str = "", session: str = "",
             now: int | None = None) -> dict:
    """Spend ``cost`` free messages for this wallet, atomically.

    Counted **before** the product runs the turn, so a crash cannot hand out a
    sixth message. Raises :class:`MembershipRequired` (402) when the allowance is
    gone -- that is the one failure a product must show as "subscribe", with the
    price and the two calls that fix it in ``details``.
    """
    wanted = str(address or "").strip()
    if not wanted:
        raise ValidationError("use_free needs the wallet's TOR1...onion address")
    spend = max(1, int(cost or 1))
    moment = int(now if now is not None else time.time())
    limit = free_limit()
    with FREE_LOCK:
        wallets = _free_book()
        row = dict(wallets.get(wanted) or {})
        used = int(row.get("used") or 0)
        if used + spend > limit:
            raise MembershipRequired(
                f"this wallet has used its {limit} free messages - the "
                f"membership is ${usd_price():.2f} per {period_days()} days in TOR",
                details={"address": wanted, "limit": limit, "used": used,
                         "left": max(0, limit - used),
                         "price_usd": usd_price(), "days": period_days(),
                         "subscribe": f"POST /membership/{wanted}/invoices",
                         "settle": f"POST /membership/{wanted}/settle"})
        row.update({"used": used + spend, "limit": limit,
                    "first_used_unix": int(row.get("first_used_unix") or moment),
                    "last_used_unix": moment,
                    "last_product": str(product or "")[:40],
                    "last_session": str(session or "")[:40],
                    "last_note": str(note or "")[:120]})
        turns = list(row.get("turns") or [])
        turns.append({"at": moment, "cost": spend,
                      "product": str(product or "")[:40],
                      "session": str(session or "")[:40],
                      "note": str(note or "")[:120]})
        row["turns"] = turns[-50:]          # the last 50 are enough to audit
        wallets[wanted] = row
        _write_free_book(wallets)
    return {"standard": STANDARD, "address": wanted, "granted": True,
            "cost": spend, "used": int(row["used"]), "limit": limit,
            "left": max(0, limit - int(row["used"])),
            "exhausted": int(row["used"]) >= limit, "at": moment,
            "product": row.get("last_product") or "",
            "now": _iso(moment)}


# ------------------------------------------------- what a product asks the chain
def identity(address: str, *, now: int | None = None,
             product: str = "") -> dict:
    """One wallet, one answer, in the shape the three products read.

    This is the only call a paid product needs on start-up: ``active``,
    ``until`` (ISO), ``plan``, ``free_messages_left``, ``price_usd`` and
    ``currency``. Everything else is context. It reports; it never gates.

    ``active`` comes from the row the ``membership`` contract wrote
    (:func:`status`), and the ``contract`` block says whether that program is
    live on this node.
    """
    moment = int(now if now is not None else time.time())
    row = status(address, now=moment)
    free = free_state(address)
    listed = plan()
    try:
        priced = quote(1)
    except Exception:  # noqa: BLE001 - a silent price feed must not blind status
        priced = {}
    return {"standard": STANDARD, "address": row["address"],
            "wallet": row.get("wallet") or "",
            "member": bool(row.get("member")), "active": bool(row.get("active")),
            "plan": row.get("plan"), "label": row.get("label"),
            "until": _iso(row["expires_at"]) if row.get("expires_at") else None,
            "expires_at": int(row.get("expires_at") or 0),
            "expires_in_seconds": int(row.get("expires_in_seconds") or 0),
            "days_left": row.get("days_left"),
            "payments": int(row.get("payments") or 0),
            "paid_micro": int(row.get("paid_micro") or 0),
            "paid_tor": row.get("paid_tor") or "0",
            "last_txid": row.get("last_txid"),
            "price_usd": listed["usd"], "price_days": listed["days"],
            "currency": "TOR", "settled_in": listed["settled_in"],
            "quote": ({"usd": priced.get("usd"),
                       "tor_usd": priced.get("tor_usd"),
                       "tor": priced.get("tor"), "micro": priced.get("micro"),
                       "amount_tor": priced.get("amount_tor")}
                      if priced else None),
            "treasury": listed["treasury"],
            "free_messages_limit": free["limit"],
            "free_messages_used": free["used"],
            "free_messages_left": free["left"],
            "free_messages_exhausted": free["exhausted"],
            "contract": contract(), "product": str(product or ""),
            "checked_at": _iso(moment),
            "subscribe": {"invoices": f"POST /membership/{row['address']}/invoices",
                          "settle": f"POST /membership/{row['address']}/settle",
                          "cancel": f"POST /membership/{row['address']}/cancel",
                          "free": f"POST /membership/{row['address']}/use-free"}}


def subscribe(address: str, *, months: float = 1, privacy: object = None,
              note: str = "", product: str = "") -> dict:
    """Open an invoice bound to this wallet, with the exact payment receipt.

    No money moves here. The answer carries the TOR amount (frozen), the
    treasury, the memo, and the node calls that settle it -- so a product can
    sign the transfer itself and the chain never needs the keystore.
    """
    wanted = str(address or "").strip()
    if not wanted:
        raise ValidationError("subscribe needs the wallet's TOR1...onion address")
    opened = create_invoice(wallet="", address=wanted, months=months,
                            note=note or f"{product} membership",
                            privacy=privacy)
    invoice = opened["invoice"]
    return {**opened, "address": wanted,
            "memo": f"membership {invoice['id']}",
            "note_hint": (f"put `membership {invoice['id']}` in the transfer's "
                          "note so the settlement is self-describing"),
            "how": ("1) sign a TOR transfer of `micro` microTOR to `treasury` with "
                    "this wallet's Ed25519 key, 2) POST the signed object to "
                    "/api/chain/tx, 3) POST its txid to settle"),
            "settle": {"method": "POST",
                       "path": f"/membership/{wanted}/settle",
                       "body": {"invoice_id": invoice["id"], "txid": "<txid>"}}}


def _invoice_by_txid(txid: str) -> dict | None:
    for row in INVOICES.all():
        if (str(row.get("txid") or "") == str(txid or "")
                and row.get("status") == "paid"):
            return row
    return None


def verify_payment(invoice: dict, address: str, txid: str) -> dict:
    """Read the settled transfer off the ledger and check it is *this* payment.

    Nothing is activated before this passes: the record must exist on the node,
    be a native TOR transfer, name this invoice's treasury as the receiver, carry
    at least the frozen amount, be signed by the subscribing wallet, and not
    predate the invoice. A txid that already settled another invoice is refused,
    so one transfer can never buy two periods.
    """
    wanted = str(address or "").strip()
    reference = str(txid or "").strip()
    if not reference:
        raise ValidationError("settle needs the txid of the payment")
    reused = _invoice_by_txid(reference)
    if reused and str(reused.get("id")) != str(invoice.get("id")):
        raise ValidationError(
            f"that transaction already settled invoice {reused.get('id')} - one "
            "payment buys one period",
            details={"txid": reference, "invoice": reused.get("id")})
    try:
        reply = node.tx(reference) or {}
    except Exception as problem:  # noqa: BLE001 - the node's words matter here
        raise ChainUnavailable(
            "the node could not answer for that txid - nothing was activated",
            details={"txid": reference,
                     "cause": str(getattr(problem, "message", problem))[:200]})
    record = reply.get("data") if isinstance(reply.get("data"), dict) else reply
    if not isinstance(record, dict) or not record.get("txid"):
        raise ValidationError("that transaction is not on this node",
                              details={"txid": reference})
    kind = str(record.get("kind") or "")
    if kind != "transfer":
        raise ValidationError(f"a {kind or 'unknown'} record cannot pay a membership",
                              details={"txid": reference, "kind": kind})
    token = str(record.get("token") or "").upper()
    if token not in ("", "TOR"):
        raise ValidationError("the membership is settled in TOR, not in a token",
                              details={"txid": reference, "token": token})
    paid_to = str(record.get("to") or "")
    if paid_to != str(invoice.get("treasury") or ""):
        raise ValidationError(
            "that payment did not go to the membership treasury",
            details={"txid": reference, "paid_to": paid_to,
                     "treasury": invoice.get("treasury")})
    amount = int(record.get("amount") or 0)
    price = int(invoice.get("micro") or 0)
    if amount < price:
        raise ValidationError(
            f"that payment is {amount} microTOR, the invoice is {price}",
            details={"txid": reference, "paid_micro": amount,
                     "invoice_micro": price})
    sender = str(record.get("from") or "")
    if sender != wanted:
        raise Forbidden("this payment was signed by another wallet",
                        details={"txid": reference, "signed_by": sender,
                                 "invoice_wallet": wanted})
    stamp = int(record.get("timestamp") or 0)
    created = int(invoice.get("created_unix") or 0)
    if created and stamp and stamp + 300 < created:
        raise ValidationError(
            "that transaction predates the invoice - it cannot settle it",
            details={"txid": reference, "tx_timestamp": stamp,
                     "invoice_created": created})
    try:
        from oneness_tor.services import privacy as privacy_service
        mode = privacy_service.mode_of(record)
    except Exception:  # noqa: BLE001 - the mode is presentation, not proof
        mode = "public"
    return {"txid": reference, "kind": kind, "from": sender, "to": paid_to,
            "sent_micro": amount, "sent_tor": format_tor(amount),
            "invoice_micro": price, "height": record.get("height"),
            "timestamp": stamp, "note": record.get("note") or "",
            "privacy": mode, "anonymous": mode != "public"}


def settle(address: str, invoice_id: str, txid: str, *,
           now: int | None = None) -> dict:
    """Verify the payment on chain, then let the contract record the period.

    Idempotent on *the same* txid: repeating the call returns the receipt that
    already exists instead of extending the membership twice. A different txid
    for an invoice that is already paid is refused, and so is a txid that
    settled another invoice -- that is what makes "pay twice by accident" or
    "reuse one transfer" impossible.
    """
    wanted = str(address or "").strip()
    if not wanted:
        raise ValidationError("settle needs the wallet's TOR1...onion address")
    invoice = INVOICES.get(str(invoice_id or "").strip())
    reference = str(txid or "").strip()
    bound = str(invoice.get("address") or "").strip()
    if bound and bound != wanted:
        raise Forbidden("this invoice belongs to another wallet",
                        details={"invoice": invoice.get("id"),
                                 "invoice_wallet": bound, "caller": wanted})
    if invoice.get("status") == "paid":
        if str(invoice.get("txid") or "") != reference:
            raise ValidationError(
                f"invoice {invoice.get('id')} is already paid and settled by "
                f"{invoice.get('txid')}",
                details={"invoice": invoice.get("id"),
                         "txid": invoice.get("txid"), "received": reference})
        member = _member_row(wanted) or {}
        return {"standard": STANDARD, "invoice": invoice, "idempotent": True,
                "membership": {"standard": STANDARD, "account": member,
                               "contract": member.get("contract_call") or {},
                               "function": "already-paid"},
                "txid": reference, "height": invoice.get("height"),
                "payment": {"txid": reference, "sent_micro": invoice.get("micro"),
                            "invoice_micro": invoice.get("micro")},
                "receipt": _receipt(invoice, {"account": member})}
    if invoice.get("status") != "open":
        raise ValidationError(
            f"invoice {invoice.get('id')} is {invoice.get('status')}",
            details={"invoice": invoice.get("id")})
    payment = verify_payment(invoice, wanted, reference)
    moment = int(now if now is not None else time.time())
    member = activate(wanted, days=int(invoice["days"]),
                      amount_micro=int(invoice["micro"]), txid=reference,
                      wallet="", now=moment)
    updated = INVOICES.update(str(invoice[INVOICES.key]), status="paid",
                              paid_at=_iso(moment), txid=reference,
                              height=payment.get("height"), address=wanted,
                              privacy=payment.get("privacy") or "",
                              expires_at=(member.get("account") or {}).get(
                                  "expires_at"))
    return {"standard": STANDARD, "invoice": updated, "membership": member,
            "txid": reference, "height": payment.get("height"),
            "payment": payment, "receipt": _receipt(updated, member),
            "idempotent": False}


def cancel(address: str, *, reason: str = "", now: int | None = None) -> dict:
    """End this wallet's period now, on the contract's own rule.

    ``membership.cancel(address, now)`` sets ``expires_at = now``: the remaining
    paid days are **not** refunded, because a refund is a separate transfer the
    operator makes deliberately -- this code never invents one. A wallet with no
    membership at all is a 404, not a silent no-op.
    """
    wanted = str(address or "").strip()
    row = _member_row(wanted)
    if not row:
        raise NotFound(f"{wanted or 'that wallet'} has no membership to cancel",
                       details={"address": wanted})
    moment = int(now if now is not None else time.time())
    identifier = contract_id()
    record: dict = {"deployed": bool(identifier), "contract": identifier,
                    "function": "cancel"}
    if identifier:
        try:
            called = contracts.call(identifier, "cancel",
                                    args={"address": wanted, "now": moment},
                                    admin=True)
            record.update({"ok": True, "at": called.get("at")})
        except Exception as problem:  # noqa: BLE001 - the local row still ends
            record.update({"ok": False,
                           "error": str(getattr(problem, "message", problem))[:300]})
    account = MEMBERS.update(str(row[MEMBERS.key]), status="cancelled",
                             expires_at=moment, cancelled_at=moment,
                             cancel_reason=str(reason or "")[:200],
                             updated_at=_now(), contract_call=record)
    return {"standard": STANDARD, "address": wanted, "cancelled": True,
            "plan": account.get("plan"), "expires_at": moment,
            "until": _iso(moment), "payments": int(account.get("payments") or 0),
            "paid_micro": int(account.get("paid_micro") or 0),
            "paid_tor": format_tor(int(account.get("paid_micro") or 0)),
            "refund": "none", "contract": record,
            "note": ("the period ends now and unused days are not refunded - a "
                     "refund would be a separate payment from the treasury")}







# ------------------------------------------- "is this transfer a real payment?"
def invoice_for(address: str, invoice_id: str = "") -> dict:
    """The invoice this transfer should settle: named, newest open, or the price.

    A product that was interrupted between "wallet signed the transfer" and
    "settle" needs this: it can ask what *would* happen, naming either the
    invoice it opened or nothing at all (the newest open invoice of the wallet).
    With no invoice at all the answer is still honest -- the current plan price
    and the treasury are used, and the result says ``expectation: "current plan"``.
    """
    wanted = str(address or "").strip()
    named = str(invoice_id or "").strip()
    if named:
        return get_invoice(named)
    rows = [row for row in INVOICES.all()
            if str(row.get("address") or "") == wanted and row.get("status") == "open"]
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    if rows:
        return dict(rows[0])
    try:
        price = micro_for(usd_price(), tor_usd())
    except Exception:  # noqa: BLE001 - a silent price feed must not hide the facts
        price = 0
    return {"id": None, "address": wanted, "treasury": treasury(),
            "micro": price, "days": period_days(), "status": "expected",
            "created_unix": 0, "expectation": "current plan"}


def payment_flags(invoice: dict, address: str, txid: str) -> dict:
    """Each rule :func:`verify_payment` enforces, as a yes/no a screen can show."""
    flags = {"on_ledger": False, "kind": False, "token": False, "receiver": False,
             "amount": False, "sender": False, "unused": False,
             "not_older_than_invoice": False}
    try:
        reply = node.tx(str(txid or "")) or {}
    except Exception:  # noqa: BLE001 - "cannot answer" is simply not on the ledger
        return flags
    record = reply.get("data") if isinstance(reply.get("data"), dict) else reply
    if not isinstance(record, dict) or not record.get("txid"):
        return flags
    flags["on_ledger"] = True
    flags["kind"] = str(record.get("kind") or "") == "transfer"
    flags["token"] = str(record.get("token") or "").upper() in ("", "TOR")
    flags["receiver"] = (str(record.get("to") or "")
                         == str(invoice.get("treasury") or ""))
    flags["amount"] = (int(record.get("amount") or 0)
                       >= int(invoice.get("micro") or 0))
    flags["sender"] = str(record.get("from") or "") == str(address or "")
    reused = _invoice_by_txid(str(txid or ""))
    flags["unused"] = (not reused
                       or str(reused.get("id")) == str(invoice.get("id")))
    stamp = int(record.get("timestamp") or 0)
    created = int(invoice.get("created_unix") or 0)
    flags["not_older_than_invoice"] = not (created and stamp and stamp + 300 < created)
    return flags


def check_payment(address: str, txid: str, *, invoice_id: str = "") -> dict:
    """Say whether a transfer *would* settle this wallet's membership. Read only.

    :func:`verify_payment` stays the authority -- ``settle`` calls it, so this
    asks the same function and can therefore never say "valid" about a transfer
    that settlement would refuse. Nothing here writes a row, moves a nonce or
    activates a period: the webapp shows a green tick before the wallet presses
    anything, and the period still only starts at ``/settle``.
    """
    wanted = str(address or "").strip()
    reference = str(txid or "").strip()
    if not wanted:
        raise ValidationError("checking a payment needs the wallet's address")
    if not reference:
        raise ValidationError("checking a payment needs the txid")
    invoice = invoice_for(wanted, invoice_id)
    flags = payment_flags(invoice, wanted, reference)
    expected = {"invoice_id": invoice.get("id"),
                "treasury": invoice.get("treasury"),
                "micro": int(invoice.get("micro") or 0),
                "tor": format_tor(int(invoice.get("micro") or 0)),
                "days": int(invoice.get("days") or period_days()),
                "expectation": invoice.get("expectation") or "open invoice"}
    try:
        payment = verify_payment(invoice, wanted, reference)
    except AppError as problem:
        return {"standard": STANDARD, "address": wanted, "txid": reference,
                "valid": False, "activates": False,
                "reason": problem.message, "code": problem.code,
                "details": problem.details, "checks": flags,
                "expected": expected, "invoice": invoice.get("id"),
                "how": (f"POST /membership/{wanted}/settle with this invoice_id "
                        "and txid once the checks are all true")}
    return {"standard": STANDARD, "address": wanted, "txid": reference,
            "valid": True, "activates": False, "reason": "this transfer pays this "
            "wallet's membership", "code": None, "details": {},
            "checks": flags, "expected": expected, "invoice": invoice.get("id"),
            "payment": payment,
            "how": (f"POST /membership/{wanted}/settle with this invoice_id and "
                    "txid to activate it - this call activated nothing")}


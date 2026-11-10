"""Invoices and settlement — every payment is a real signed TOR transfer.

The price always comes from :mod:`core.pricing` (the LOCKED table), the
destination is always the node's fee receiver, and ``pay_invoice`` broadcasts a
genuine Ed25519 transaction before it flips the invoice to ``paid``.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from core import pricing
from core.errors import ValidationError
from core.node_client import node
from core.validator import format_tor, to_micro
from oneness_tor.services import ledger
from oneness_tor.services.store import Collection, data_dir

INVOICES = Collection(lambda: data_dir("payments") / "invoices.json", "inv")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plans() -> list[dict]:
    rows = []
    for row in pricing.table():
        plan = {"code": row["code"], "label": row["label"], "tor": row["tor"],
                "micro_tor": row["micro_tor"], "locked": row["locked"],
                "collected_by": row["collected_by"]}
        rows.append(plan)
    return rows


def quote(code: str, quantity: float = 1) -> dict:
    spec = pricing.spec(code)
    quantity = float(quantity or 1)
    if quantity <= 0:
        raise ValidationError("quantity must be greater than zero")
    amount_micro = pricing.micro(code, quantity)
    return {"code": code, "label": spec["label"], "quantity": quantity,
            "tor": round(spec["tor"] * quantity, 8),
            "micro_tor": amount_micro,
            "amount_tor": format_tor(amount_micro),
            "locked": spec["locked"]}


def create_invoice(*, code: str | None = None, amount_tor: float | None = None,
                   wallet: str | None = None, note: str = "",
                   quantity: float = 1) -> dict:
    if code:
        quote_row = quote(code, quantity)
        amount_micro = quote_row["micro_tor"]
        label = quote_row["label"]
    elif amount_tor is not None:
        amount_micro = to_micro(float(amount_tor))
        if amount_micro <= 0:
            raise ValidationError("amount_tor must be greater than zero")
        label, code = "Custom amount", "custom"
    else:
        raise ValidationError("code or amount_tor is required")
    address = node.fee_receiver()
    invoice = INVOICES.add({
        "code": code, "label": label, "quantity": float(quantity),
        "amount_micro": int(amount_micro), "amount_tor": format_tor(amount_micro),
        "address": address, "status": "open", "wallet": wallet or "",
        "note": str(note or "")[:200], "created_at": _now(),
        "created_unix": int(time.time()), "paid_at": None, "txid": None,
        "height": None})
    return invoice


def list_invoices(limit: int = 25, status: str | None = None) -> dict:
    rows = INVOICES.all()
    if status:
        rows = [row for row in rows if row.get("status") == status]
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    rows = rows[:max(1, int(limit))]
    return {"invoices": rows,
            "totals": {"open": sum(1 for row in INVOICES.all()
                                   if row.get("status") == "open"),
                       "paid": sum(1 for row in INVOICES.all()
                                   if row.get("status") == "paid")},
            "count": len(rows)}


def get_invoice(invoice_id: str) -> dict:
    return INVOICES.get(invoice_id)


def pay_invoice(invoice_id: str, wallet_name: str) -> dict:
    invoice = INVOICES.get(invoice_id)
    if invoice.get("status") == "paid":
        raise ValidationError(f"invoice {invoice_id} is already paid",
                              details={"txid": invoice.get("txid")})
    if invoice.get("status") != "open":
        raise ValidationError(f"invoice {invoice_id} is {invoice.get('status')}")
    result = ledger.send_micro(str(wallet_name), invoice["address"],
                               int(invoice["amount_micro"]),
                               note=f"invoice {invoice_id} {invoice['code']}")
    updated = INVOICES.update(invoice_id, status="paid", paid_at=_now(),
                              txid=result.get("txid"),
                              height=result.get("height"),
                              wallet=str(wallet_name))
    return {"invoice": updated, "txid": result.get("txid"),
            "height": result.get("height"), "settlement": result}


def revenue() -> dict:
    paid = [row for row in INVOICES.all() if row.get("status") == "paid"]
    by_code: dict[str, dict] = {}
    for row in paid:
        code = str(row.get("code") or "custom")
        bucket = by_code.setdefault(code, {"code": code, "count": 0,
                                           "micro_tor": 0})
        bucket["count"] += 1
        bucket["micro_tor"] += int(row.get("amount_micro") or 0)
    for bucket in by_code.values():
        bucket["tor"] = format_tor(bucket["micro_tor"])
    total = sum(bucket["micro_tor"] for bucket in by_code.values())
    return {"total_micro": total, "total_tor": format_tor(total),
            "count": len(paid),
            "by_code": sorted(by_code.values(), key=lambda b: -b["micro_tor"])}

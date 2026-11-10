"""The admin panel's fee schedule — every service priced by the operator.

``core.pricing`` owns the numbers and the fee router charges them; this module is
the admin surface over that table: read it with its defaults for the UI, apply
one or many new prices, reset a row (or all of them) to the published value.

Nothing here charges anything, and nothing here caches a price: a saved value is
in ``data/admin/pricing.json`` and the very next ``pricing.micro(...)`` call in
this process reads it, so the panel cannot drift from what customers are billed.
"""
from __future__ import annotations

from core import pricing
from core.errors import ValidationError


def table() -> dict:
    """The live fee schedule, its defaults, and the change history."""
    return pricing.admin_table()


def update(items: object, *, confirm_locked: bool = False,
           actor: str = "admin") -> dict:
    """Reprice one or many services.

    ``items`` is ``{code: tor}`` (the panel's shape) or a list of
    ``{code, tor}`` rows. It is applied row by row so a single bad code fails
    with the code's own message instead of a half-written table — every row that
    did apply is kept and reported in ``applied``.
    """
    if isinstance(items, dict):
        pairs = list(items.items())
    elif isinstance(items, list):
        pairs = [(row.get("code"), row.get("tor")) for row in items
                 if isinstance(row, dict)]
    else:
        raise ValidationError(
            'send {"prices": {"<code>": <tor in TOR>}} or one {"code": ..., "tor": ...}',
            details={"codes": sorted(pricing.PRICE_TABLE)})
    if not pairs:
        raise ValidationError("no prices were sent",
                              details={"codes": sorted(pricing.PRICE_TABLE)})
    applied = []
    for code, tor in pairs:
        applied.append(pricing.set_price(str(code or ""), tor,
                                         confirm_locked=confirm_locked,
                                         actor=actor))
    document = pricing.admin_table()
    return {**document, "applied": applied,
            "applied_codes": [row["code"] for row in applied]}


def reset(code: str | None = None, *, actor: str = "admin") -> dict:
    """Reset one row (``code``) or the whole table to the published prices."""
    if code:
        row = pricing.reset_price(code, actor=actor)
        return {**pricing.admin_table(), "removed": row,
                "message": (f"{code} is back to "
                            f"{row['tor']:g} TOR ({row['source']})")}
    return pricing.reset_all(actor=actor)


def quote(code: str) -> dict:
    """One row at its live price — safe to show before anyone signs in."""
    row = pricing.spec(code)
    return {"code": code, "label": row["label"], "tor": row["tor"],
            "micro_tor": pricing.micro(code), "locked": row["locked"],
            "default_tor": row["default_tor"], "source": row["source"],
            "collected_by": row["collected_by"]}

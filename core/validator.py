"""One validator used by every API, page, contract and the minter (Section 8).

Address rules are LOCKED: ``^TOR1[a-z2-7]{30,48}onion$``. The native coin id is
``TOR111111111111111111111111111111onion`` and is never a transfer destination.
"""
from __future__ import annotations

import re

from core.errors import ValidationError

ADDRESS_PATTERN = r"^TOR1[a-z2-7]{30,48}onion$"
_ADDRESS_RE = re.compile(ADDRESS_PATTERN)
NATIVE_COIN_ID = "TOR111111111111111111111111111111onion"
TOKEN_ADDRESS_EXAMPLES = (
    "TOR1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAonion",
    "TOR1DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDonion",
)
TOR_DECIMALS = 8
MICRO_PER_TOR = 10 ** TOR_DECIMALS
MAX_TOKEN_ID = 2 ** 64 - 1


def is_address(value: object) -> bool:
    """True when ``value`` is a well-formed TOR address."""
    return bool(_ADDRESS_RE.match(str(value or "").strip()))


def validate_address(value: object, field: str = "address",
                     *, allow_native: bool = False) -> str:
    """Return the trimmed address or raise :class:`ValidationError`."""
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required")
    if not _ADDRESS_RE.match(text):
        raise ValidationError(
            f"{field} is not a valid TOR address — expected the form "
            f"TOR1…onion (regex {ADDRESS_PATTERN})",
            details={"field": field, "received": text[:72],
                     "examples": list(TOKEN_ADDRESS_EXAMPLES)})
    if text == NATIVE_COIN_ID and not allow_native:
        raise ValidationError(
            f"{field} cannot be the native coin id {NATIVE_COIN_ID}")
    return text


def validate_token_id(value: object, field: str = "token_id") -> int:
    """Token ids are unsigned 64-bit integers (``#12`` in the UI)."""
    text = str(value if value is not None else "").strip().lstrip("#")
    if not text:
        raise ValidationError(f"{field} is required")
    if not text.isdigit():
        raise ValidationError(f"{field} must be a whole number")
    number = int(text)
    if number > MAX_TOKEN_ID:
        raise ValidationError(f"{field} is out of range (max {MAX_TOKEN_ID})")
    return number


def to_micro(amount_tor: float) -> int:
    """0.1 TOR -> 10_000_000 microTOR, exactly (no float drift)."""
    return int(round(float(amount_tor) * MICRO_PER_TOR))


def from_micro(micro: int) -> float:
    return round(int(micro) / MICRO_PER_TOR, TOR_DECIMALS)


def format_tor(micro: int) -> str:
    """'12.5' style display string with trailing zeros trimmed."""
    text = f"{from_micro(micro):.{TOR_DECIMALS}f}".rstrip("0").rstrip(".")
    return text or "0"


def validate_amount(micro: int, field: str = "amount") -> int:
    if int(micro) <= 0:
        raise ValidationError(f"{field} must be greater than zero")
    return int(micro)

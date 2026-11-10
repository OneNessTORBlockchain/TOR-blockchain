"""Per-transaction privacy — the wallet chooses PUBLIC or ANONYMOUS before every send.

Protocol ``oneness-tor/privacy/1``

What changed and why
--------------------
The old rule was time-based: a transfer stayed public for 30 days and was only
sealed private after that. It made the sender's privacy a function of the
calendar — the exact 30 days in which a counterparty, a screener or a scraper is
most interested were the 30 days the address was on display.

That window no longer exists: the ledger keeps every record forever and seals
nothing. The rule is per transaction instead, and the *wallet asks*:

* **PUBLIC** — the sending wallet's address is written to the record and is
  visible on every explorer, API and page, permanently.
* **ANONYMOUS** — the record shows :func:`label` instead of the address
  (``Tor-Anonymous-onion`` by default), from the first confirmation onward.

How it rides on the chain without a rewrite
-------------------------------------------
The flag is stamped into the transaction's ``external_ref`` field, which the
Rust ledger already copies into the record it publishes
(``rust/torchain/src/chain.rs`` — ``tx_transfer`` → ``make_record``). That field
is *not* part of the canonical signing message
(``oneness-tor:v1|kind|from|to|amount|token|note|nonce|timestamp``), so:

* no new transaction kind, no new struct field, no state-root or block-hash
  change — a node running the already-committed ``torchain`` binary accepts and
  stores it,
* every legacy record (``external_ref: ""``) reads back as ``public``, which is
  exactly what it was,
* only the wallet that signs a transaction can mark it anonymous: it is its own
  address it hides.

A *public* send writes the empty ref, so a public transfer is byte-identical to
a legacy one.

What is hidden, and what is not (read this before claiming privacy)
------------------------------------------------------------------
Hidden on every public read path: the sending address, and nothing else. The
receiver is never masked — a payment must still name the wallet that receives it,
and that address belongs to somebody else, not to the sender who chose privacy.
Not hidden either: the amount, the token, the fee, the timestamp, the block, the
txid — and the **balance sheet**, because balances on this chain are public and
permanent by design (``/api/public/balances``). A patient observer who watches
balances move block by block can still correlate a transfer. That is the honest
limit of a transparent-account chain: this is *address masking on the record*,
not cryptographic untraceability. Ring signatures / shielded pools would be
``oneness-tor/privacy/2`` and are not what this module claims.

Enforcement points
------------------
:mod:`oneness_tor.services.ledger` writes the mode, and every public projection
masks through :func:`public_view`:

* ``GET /api/public/records``, ``GET /api/chain/events``,
  ``GET /api/chain/tx/<txid>`` (the chain admin still sees the truth),
* ``GET /api/privacy`` publishes the rule,
* the wallet page asks before each send and the explorer labels the row.

One more honest limit: the Rust node's own HTTP port answers
``GET /api/public/records`` without masking. A node started by this repository
binds it to loopback (``torchain serve --bind 127.0.0.1:8545``,
``core.ledger_host.bind_address``), so the Python API is the only public face —
but an operator who exposes that port to the internet publishes every address,
anonymous or not.
"""
from __future__ import annotations

from core.config import settings
from core.errors import ValidationError

#: The protocol string every receipt, page and document quotes.
STANDARD = "oneness-tor/privacy/1"

PUBLIC = "public"
ANONYMOUS = "anonymous"
MODES: tuple[str, ...] = (PUBLIC, ANONYMOUS)

#: What an anonymous sender is called on chain (``privacy.anonymous_label``).
ANONYMOUS_LABEL = "Tor-Anonymous-onion"

#: Every spelling a wallet, a CLI or an API client may send.
_ALIASES: dict[str, str] = {
    PUBLIC: PUBLIC, "open": PUBLIC, "named": PUBLIC, "visible": PUBLIC,
    "public_on_chain": PUBLIC,
    ANONYMOUS: ANONYMOUS, "anon": ANONYMOUS, "private": ANONYMOUS,
    "hidden": ANONYMOUS, "onion": ANONYMOUS, "masked": ANONYMOUS,
    ANONYMOUS_LABEL.lower(): ANONYMOUS,
}

#: ``external_ref`` value shape: ``oneness-privacy:1|mode=anonymous``.
_REF_PREFIX = "oneness-privacy:1"
_REF_KEYS = ("mode", "privacy")


# ------------------------------------------------------------------ the knobs
def label() -> str:
    """The name an anonymous sender appears under on chain."""
    return str(settings.get("privacy.anonymous_label") or "").strip() \
        or ANONYMOUS_LABEL


def default_mode() -> str:
    """What a send does when the caller says nothing (``privacy.default_mode``).

    Defaults to ``public``: a node never silently hides a record the operator
    did not ask to hide, and a misspelt config value falls back instead of
    turning every transfer anonymous.
    """
    return _ALIASES.get(str(settings.get("privacy.default_mode") or "").strip().lower(),
                        PUBLIC)


# ------------------------------------------------------------------- the mode
def normalize(value: object = None) -> str:
    """``"public"`` / ``"anonymous"`` from any accepted spelling.

    An empty value means "the wallet did not choose", which is
    :func:`default_mode`. Anything else that is not a known mode is a
    ``VALIDATION_ERROR`` — a typo must never quietly publish an address the
    sender believed was hidden.
    """
    if isinstance(value, bool):
        raise ValidationError(
            "privacy must be 'public' or 'anonymous', not a boolean",
            details={"received": value, "accepted": list(MODES)})
    text = str(value or "").strip().lower()
    if not text:
        return default_mode()
    mode = _ALIASES.get(text)
    if mode is None:
        raise ValidationError(
            "privacy must be 'public' or 'anonymous'",
            details={"received": text[:32], "accepted": list(MODES),
                     "hint": ("the wallet asks before every transaction: public "
                              "puts your address on chain, anonymous shows "
                              f"{label()} instead")})
    return mode


def chain_ref(value: object = None) -> str:
    """The ``external_ref`` this mode writes into the transaction.

    Public sends write ``""`` (byte-identical to every legacy transfer), so the
    only thing on chain is the *anonymous* declaration — which is also the only
    thing that has to survive a node restart.
    """
    mode = normalize(value)
    return "" if mode == PUBLIC else f"{_REF_PREFIX}|mode={mode}"


def mode_of(record: object) -> str:
    """The mode a chain record was written with — legacy records are public."""
    if not isinstance(record, dict):
        return PUBLIC
    declared = record.get("privacy")
    if isinstance(declared, str) and declared.strip():
        parsed = _ALIASES.get(declared.strip().lower())
        if parsed:
            return parsed
    ref = str(record.get("external_ref") or "").strip().lower()
    if not ref:
        return PUBLIC
    if ref in _ALIASES:
        return _ALIASES[ref]
    for chunk in ref.replace(";", "|").replace(",", "|").split("|"):
        name, _, value = chunk.partition("=")
        if name.strip() in _REF_KEYS:
            parsed = _ALIASES.get(value.strip())
            if parsed:
                return parsed
    return PUBLIC


def is_anonymous(record: object) -> bool:
    """True when this record hides its sender."""
    return mode_of(record) == ANONYMOUS


# ------------------------------------------------------------------ masking
def mask(address: object, mode: object = PUBLIC) -> str:
    """``address`` when the mode is public, :func:`label` when it is anonymous."""
    text = str(address or "")
    return label() if normalize(mode) == ANONYMOUS and text else text


def public_view(record: dict) -> dict:
    """One record as the public may see it — the masking rule, in one place.

    Only ``from`` is ever replaced: an anonymous send hides the address of the
    wallet that signed it, which is the only address that wallet owns. ``to`` is
    left untouched — masking the receiver would publish a *lie* about who was
    paid, and the receiver never chose to be hidden.

    The owner's own history does **not** go through here: it is authorized, so
    it shows the real addresses and reports ``on_chain_from`` next to them.
    """
    if not isinstance(record, dict):
        return record
    mode = mode_of(record)
    view = dict(record)
    hidden: list[str] = []
    if mode == ANONYMOUS:
        if str(view.get("from") or ""):
            view["from"] = label()
            hidden.append("from")
    view["privacy"] = mode
    view["anonymous"] = mode == ANONYMOUS
    view["masked_fields"] = hidden
    return view


def public_view_all(records: object) -> list:
    """Mask a whole list (the ``records`` / ``events`` arrays) in one pass."""
    if not isinstance(records, list):
        return []
    return [public_view(row) for row in records if isinstance(row, dict)]


# --------------------------------------------------------------- the document
def policy() -> dict:
    """``GET /api/privacy`` — the rule, what it hides, and what it cannot."""
    return {
        "standard": STANDARD,
        "modes": list(MODES),
        "default_mode": default_mode(),
        "chosen": ("before every transaction: the wallet asks Public or "
                   "Anonymous, and the answer is stamped on that one record"),
        "anonymous_label": label(),
        "chain_field": "external_ref",
        "chain_value_for_anonymous": chain_ref(ANONYMOUS),
        "chain_value_for_public": chain_ref(PUBLIC),
        "how_to_choose": {
            "wallet_page": "/wallet → send → privacy",
            "wallet_api": ('POST /api/wallets/<name>/send {"to": ..., '
                           '"amount": ..., "privacy": "public" | "anonymous"}'),
            "sign_only": ('POST /api/wallets/<name>/sign {"privacy": "anonymous", '
                          '"..."} — the ref travels inside signed_tx'),
            "client": ("any signed transaction whose external_ref is "
                       f"{chain_ref(ANONYMOUS)}"),
        },
        "hides": ["from (the sending wallet's own address)"],
        "never_masked": [
            "to (the receiver's address always stays on the record — a sender "
            "can only hide its own address, never somebody else's)",
        ],
        "shows": ["amount", "token", "fee", "note", "timestamp", "height",
                  "block", "txid", "signature"],
        "still_public": {
            "balances": ("the balance sheet is public and permanent by design — "
                         "GET /api/public/balances, /api/public/ledger, "
                         "/api/public/totals"),
            "consequence": ("a patient observer who watches balances move block "
                            "by block can still correlate a transfer"),
        },
        "not_cryptographic": (
            "this is address masking on the public record, not untraceability: "
            "the ledger must know the signer to debit it, and the node operator "
            "can read it. Ring signatures / shielded pools would be "
            "oneness-tor/privacy/2 and are not implemented"),
        "enforced_by": [
            "oneness_tor.services.privacy.public_view (every public projection)",
            "GET /api/public/records, GET /api/chain/events, "
            "GET /api/chain/tx/<txid>",
            "the owner (wallet session or admin token) still reads the unmasked "
            "record",
        ],
        "public_paths_masked": ["/api/public/records", "/api/chain/events",
                                "/api/chain/tx/<txid>"],
        "owner_paths_unmasked": [
            "/api/wallets/<name>/history (wallet session or admin)",
            "POST /api/private/records (chain admin)"],
    }

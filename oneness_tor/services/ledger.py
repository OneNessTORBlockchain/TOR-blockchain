"""Real wallet + transfer operations against the Rust ledger.

Signing uses the same canonical message the node verifies
(``oneness-tor:v1|kind|from|to|amount|token|note|nonce|timestamp``) through
:func:`tools.torchain_signer.sign_transaction`, so every transfer this API
broadcasts is a genuine Ed25519 transaction, not a simulation.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import base58
import segno

from core import pricing
from core.config import settings
from core.errors import ChainUnavailable, NotFound, ValidationError
from core.node_client import node
from core.validator import MICRO_PER_TOR, format_tor, to_micro, validate_address
from oneness_tor.services import unwrap
from oneness_tor.services.privacy import PUBLIC, chain_ref, mode_of, public_view
from oneness_tor.services.privacy import label as anonymous_label
from oneness_tor.services.privacy import normalize as privacy_mode
from tools.torchain_signer import sign_transaction

WALLET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
ROLE_OF = {"founder": "founder", "user1": "holder"}


def wallet_dir() -> Path:
    path = settings.data_dir / "wallets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def wallet_names() -> list[str]:
    return sorted(path.stem for path in wallet_dir().glob("*.json")
                  if not path.stem.endswith(".public"))


def wallet_path(name: str) -> Path:
    text = str(name or "").strip()
    if not WALLET_NAME.match(text):
        raise ValidationError("wallet name must match " + WALLET_NAME.pattern,
                              details={"received": text[:64]})
    path = wallet_dir() / f"{text}.json"
    if not path.exists():
        raise NotFound(f"wallet {text!r} not found",
                       details={"available": wallet_names()})
    return path


def load_wallet(name: str) -> dict:
    return json.loads(wallet_path(name).read_text(encoding="utf-8"))


def node_account(address: str) -> dict:
    """Live account, normalised: ``balance_micro`` is always microTOR.

    The Rust node reports ``balance`` in TOR (float) **and** ``balance_micro``
    (exact). Everything in the API layer reads ``balance_micro`` so no value is
    ever rounded twice; ``balance_tor`` is the display string.
    """
    try:
        reply = node.account(address)
    except ChainUnavailable as problem:
        return {"address": address, "balance_micro": 0, "balance_tor": "0",
                "tokens": {}, "nonce": 0, "role": "unknown",
                "warning": problem.code}
    if not reply.get("ok"):
        return {"address": address, "balance_micro": 0, "balance_tor": "0",
                "tokens": {}, "nonce": 0, "role": "unknown",
                "warning": (reply.get("error") or {}).get("code")}
    raw = reply["data"]
    micro = raw.get("balance_micro")
    if micro is None:
        micro = int(round(float(raw.get("balance") or 0) * MICRO_PER_TOR))
    return {**raw, "balance_micro": int(micro),
            "balance_tor": format_tor(int(micro))}


def public_wallet(name: str, *, with_balance: bool = True) -> dict:
    wallet = load_wallet(name)
    view = {"name": name, "address": wallet.get("address"),
            "pubkey_base58": wallet.get("pubkey_base58"),
            "role": ROLE_OF.get(name, "holder"),
            "created_at": wallet.get("created_at"),
            "has_mnemonic": bool(wallet.get("mnemonic")),
            "key_file": f"data/wallets/{name}.json"}
    if with_balance:
        account = node_account(str(wallet.get("address") or ""))
        micro = int(account.get("balance_micro") or 0)
        view.update({"balance": micro,
                     "balance_micro": micro,
                     "balance_tor": account.get("balance_tor") or "0",
                     "tokens": account.get("tokens") or {},
                     "nonce": int(account.get("nonce") or 0),
                     "role": account.get("role") or view["role"]})
        if account.get("warning"):
            view["warning"] = account["warning"]
    return view


def all_wallets() -> list[dict]:
    return [public_wallet(name) for name in wallet_names()]


def balance_of(name_or_address: str) -> dict:
    address = str(name_or_address or "").strip()
    if WALLET_NAME.match(address) and (wallet_dir() / f"{address}.json").exists():
        wallet = load_wallet(address)
        return {"name": address, **node_account(str(wallet["address"]))}
    validate_address(address, "address")
    return {"name": None, **node_account(address)}



# ------------------------------------------------------------------- receive
def _b58_hex(value: object) -> str:
    """base58 → lowercase hex (``""`` when it is not base58)."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return base58.b58decode(text).hex()
    except Exception:  # noqa: BLE001 - a malformed value is reported as empty
        return ""


def _hex_text(value: object) -> str:
    """A hash that is already hex stays as-is; anything else is decoded."""
    text = str(value or "").strip().lower()
    if text and len(text) % 2 == 0 and all(ch in "0123456789abcdef" for ch in text):
        return text
    return _b58_hex(value)


def _qr(text: str, label: str) -> dict:
    """A real QR code (segno, error level M) as an inline SVG data URI."""
    if not text:
        return {"label": label, "text": "", "version": 0,
                "error_correction": "", "svg": ""}
    try:
        symbol = segno.make(text, error="m")
        svg = symbol.svg_data_uri(scale=4, border=2, dark="#04050a", light="#ffffff")
    except Exception as problem:  # noqa: BLE001 - never fail a receive panel
        return {"label": label, "text": text, "version": 0,
                "error_correction": "", "svg": "",
                "warning": f"qr not generated: {problem}"}
    return {"label": label, "text": text, "version": int(symbol.version),
            "error_correction": str(symbol.error), "svg": svg}


def receive(name: str) -> dict:
    """Address + public key + QR codes — everything a sender needs."""
    wallet = load_wallet(name)
    address = str(wallet.get("address") or "")
    pubkey = str(wallet.get("pubkey_base58") or "")
    return {"name": name, "address": address, "pubkey_base58": pubkey,
            "pubkey_hex": _b58_hex(pubkey),
            "chain_id": str(settings.get("chain.chain_id")),
            "native_coin_id": str(settings.get("chain.native_coin_id")),
            "qr": {"address": _qr(address, "receive address"),
                   "pubkey": _qr(pubkey, "public key")},
            "how": ("share the address (or its QR) to receive TOR and any token "
                    "this wallet holds; the public key lets a sender verify "
                    "the owner before paying"),
            "note": "the private key never leaves data/wallets/<name>.json"}

# ------------------------------------------------------------------- history
def _iso(stamp: int | float) -> str:
    return datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(timespec="seconds")


def _resolve_address(name_or_address: str) -> tuple[str, str | None]:
    """``(address, wallet name or None)`` for a wallet name or a raw address."""
    text = str(name_or_address or "").strip()
    if WALLET_NAME.match(text) and (wallet_dir() / f"{text}.json").exists():
        wallet = load_wallet(text)
        return str(wallet.get("address") or ""), text
    validate_address(text, "address")
    return text, None


def _record_row(record: dict, address: str) -> dict:
    """One ledger record as the wallet page shows it (hex included).

    ``from``/``to`` are the real addresses because this view is authorized (a
    wallet session or the admin token earned it). ``on_chain_from`` /
    ``on_chain_to`` are what the *public* sees for the same record, so an
    anonymous send is labelled honestly instead of looking like a plain one.
    """
    sender = str(record.get("from") or "")
    recipient = str(record.get("to") or "")
    mode = mode_of(record)
    shown = public_view(record)
    if sender == address and recipient == address:
        direction = "self"
    elif sender == address:
        direction = "out"
    else:
        direction = "in"
    token = str(record.get("token") or "").upper() or None
    amount = int(record.get("amount") or 0)
    fee = int(record.get("fee") or 0)
    return {"txid": record.get("txid"),
            "txid_hex": _hex_text(record.get("txid")),
            "kind": record.get("kind"),
            "direction": direction,
            "from": sender, "to": recipient,
            "privacy": mode, "anonymous": mode != PUBLIC,
            "on_chain_from": str(shown.get("from") or ""),
            "on_chain_to": str(shown.get("to") or ""),
            "token": token,
            "unit": "token" if token else "microTOR",
            "amount": amount,
            "amount_tor": None if token else format_tor(amount),
            "fee_micro": fee, "fee_tor": format_tor(fee),

            "note": record.get("note") or "",
            "signature": record.get("signature") or "",
            "signature_hex": _hex_text(record.get("signature")),
            "height": record.get("height"),
            "seq": record.get("seq"),
            "block_hash": record.get("block_hash") or "",
            "block_hash_hex": _hex_text(record.get("block_hash")),
            "timestamp": record.get("timestamp"),
            "explorer": f"/api/chain/tx/{record.get('txid')}"}


def history(name_or_address: str, *, limit: int = 2000) -> dict:
    """Every transaction this wallet appears in, newest first.

    The ledger keeps every record and never expires one, so this list is the
    wallet's whole history rather than a window of it. ``limit`` bounds how many
    records are scanned; ``truncated`` says when that bound was hit. Every row
    carries the txid, signature and block hash in hex.
    """
    address, name = _resolve_address(name_or_address)
    limit = max(1, min(2000, int(limit)))

    reply = node.public_records(limit)
    data = (reply.get("data") or {}) if reply.get("ok") else {}
    scanned = data.get("records") or []

    rows: list[dict] = []
    for record in scanned:
        if not isinstance(record, dict):
            continue
        sender = str(record.get("from") or "")
        recipient = str(record.get("to") or "")
        if address not in (sender, recipient):
            continue
        rows.append(_record_row(record, address))
    rows.sort(key=lambda row: (int(row.get("timestamp") or 0),
                               int(row.get("seq") or 0)), reverse=True)

    totals = {"in_tor": 0.0, "out_tor": 0.0, "fee_tor": 0.0, "tokens": {}}
    for row in rows:
        if row["token"]:
            bucket = totals["tokens"].setdefault(row["token"], {"in": 0.0, "out": 0.0})
            bucket["in" if row["direction"] == "in" else "out"] += float(row["amount"])
        else:
            key = "in_tor" if row["direction"] == "in" else "out_tor"
            totals[key] = round(totals[key] + row["amount"] / 1e8, 8)
        totals["fee_tor"] = round(totals["fee_tor"] + row["fee_micro"] / 1e8, 8)

    return {"address": address, "wallet": name,
            "count": len(rows), "scanned": len(scanned),
            "limit": limit, "truncated": len(scanned) >= limit,
            "records": rows, "totals": totals,
            "note": ("the ledger keeps every record and expires none, so this is "
                     "the wallet's whole history (raise the scan limit to reach "
                     "further back)")}



def _submit(wallet: dict, tx: dict) -> dict:
    signed = sign_transaction(wallet, tx)
    data = unwrap(node.submit_tx(signed))
    records = data.get("records") or []
    match = next((record for record in records
                  if record.get("kind") == tx["kind"]),
                 records[0] if records else {})
    account = node_account(str(wallet["address"]))
    balance = int(account.get("balance_micro") or 0)
    return {"txid": match.get("txid"), "kind": tx["kind"],
            "height": data.get("height"), "head_hash": data.get("head_hash"),
            "accepted": data.get("accepted"), "records": records,
            "from": wallet["address"], "to": tx.get("to", ""),
            "amount_micro": tx["amount"], "token": tx.get("token"),
            "nonce": tx["nonce"], "timestamp": tx["timestamp"],
            "balance": balance, "balance_micro": balance,
            "balance_tor": format_tor(balance)}


def _platform_fee(wallet: dict, *, nonce: int, privacy: object = None) -> dict:
    """The node/platform fee — the chain itself charges nothing.

    It is a real, separate signed transfer to the node's fee receiver, so it
    shows up in the ledger like any other transfer. When the payer *is* the fee
    receiver the fee would be a no-op, so it is skipped and reported as waived.
    The fee carries the *same* privacy mode as the transfer it belongs to: a
    public 0.1 TOR fee would otherwise re-publish the sender of an anonymous
    transaction.
    """
    mode = privacy_mode(privacy)
    amount = pricing.micro("transfer")
    sender = str(wallet["address"])
    if amount <= 0:
        return {"amount_micro": 0, "amount_tor": "0", "waived": True,
                "reason": "the node/platform fee is disabled"}
    try:
        receiver = node.fee_receiver()
    except Exception as problem:  # noqa: BLE001 - never hide the transfer itself
        return {"amount_micro": 0, "amount_tor": "0", "waived": True,
                "reason": f"fee receiver unavailable: {getattr(problem, 'message', problem)}"}
    if receiver == sender:
        return {"amount_micro": 0, "amount_tor": "0", "waived": True,
                "reason": "the payer is the fee receiver, so the fee is a no-op"}
    submitted = _submit(wallet, {
        "kind": "transfer", "from": sender, "to": receiver,
        "amount": int(amount), "token": None,
        "note": "node/platform transfer fee", "external_ref": chain_ref(mode),
        "nonce": int(nonce), "timestamp": int(time.time())})
    return {"amount_micro": int(amount), "amount_tor": format_tor(amount),
            "receiver": receiver, "txid": submitted.get("txid"),
            "privacy": mode, "waived": False}


def send_micro(name: str, to: str, amount_micro: int, *, note: str = "",
               token: str | None = None, kind: str = "transfer",
               platform_fee: bool = True, privacy: object = None) -> dict:
    """Sign + broadcast; ``amount_micro`` is microTOR (or whole token units).

    ``privacy`` is the wallet's per-transaction answer — ``"public"`` puts this
    wallet's address on chain, ``"anonymous"`` puts
    :func:`oneness_tor.services.privacy.label` there instead. It is normalised
    *before* anything is signed, so a typo can never publish an address the
    sender believed was hidden.
    """
    from oneness_tor.services import node_sync

    mode = privacy_mode(privacy)
    node_sync.require_writable()
    wallet = load_wallet(name)
    sender = str(wallet["address"])
    validate_address(to, "to")
    if int(amount_micro) <= 0:
        raise ValidationError("amount must be greater than zero")
    account = node_account(sender)
    nonce = int(account.get("nonce") or 0)
    tx = {"kind": kind, "from": sender, "to": to,
          "amount": int(amount_micro), "token": token or None,
          "note": str(note or "")[:200], "external_ref": chain_ref(mode),
          "nonce": nonce, "timestamp": int(time.time())}
    result = _submit(wallet, tx)
    if platform_fee and kind == "transfer":
        try:
            result["platform_fee"] = _platform_fee(wallet, nonce=nonce + 1,
                                                   privacy=mode)
        except Exception as problem:  # noqa: BLE001 - the transfer already landed
            result["platform_fee"] = {
                "amount_micro": 0, "waived": True,
                "reason": f"fee not charged: {getattr(problem, 'message', problem)}"}
        if result["platform_fee"].get("txid"):
            account = node_account(sender)
            result["balance"] = int(account.get("balance_micro") or 0)
            result["balance_micro"] = result["balance"]
            result["balance_tor"] = format_tor(result["balance"])
    result["privacy"] = mode
    result["anonymous"] = mode != PUBLIC
    result["on_chain_from"] = anonymous_label() if mode != PUBLIC else sender
    return result


def send(name: str, to: str, *, amount_tor: float | None = None,
         amount_token: float | None = None, token: str | None = None,
         note: str = "", privacy: object = None) -> dict:
    """Human-facing transfer: TOR is decimal, token amounts are whole units."""
    mode = privacy_mode(privacy)
    if token or amount_token is not None:
        if amount_token is None:
            raise ValidationError("amount_token is required for a token transfer")
        result = send_micro(name, to, int(amount_token), note=note,
                            token=str(token or "").upper(), kind="transfer",
                            privacy=mode)
        result["amount"] = int(amount_token)
        return result
    if amount_tor is None:
        raise ValidationError("amount (TOR) is required")
    micro = to_micro(float(amount_tor))
    result = send_micro(name, to, micro, note=note, privacy=mode)
    result["amount"] = micro
    return result


def charge(wallet_name: str, code: str, *, quantity: float = 1,
           note: str = "") -> dict:
    """Charge a LOCKED price from ``wallet_name`` to the node's fee receiver."""
    spec = pricing.spec(code)
    amount_micro = pricing.micro(code, quantity)
    receiver = node.fee_receiver()
    memo = f"{code}: {note}".strip().rstrip(":").strip()
    result = send_micro(wallet_name, receiver, amount_micro, note=memo,
                        platform_fee=False)
    return {"code": code, "label": spec["label"], "tor": spec["tor"],
            "amount_micro": amount_micro, "amount_tor": format_tor(amount_micro),
            "fee_receiver": receiver, **result}


def _runnable(path: str) -> str:
    """POSIX: an artifact or zip checkout that lost the exec bit still runs."""
    if os.name != "nt":
        try:
            mode = os.stat(path).st_mode
            if not mode & 0o111:
                os.chmod(path, mode | 0o755)
        except OSError:
            pass
    return path


def binary_path() -> str:
    """The ``torchain`` executable *this* host can run — never one hardcoded path.

    :func:`core.ledger_host.find_binary` is the single resolver, and it knows all
    four places a ledger can live: ``ONENESS_NODE_BINARY`` / ``node.binary`` →
    ``chain.binary`` (``rust/torchain/target/release/torchain``, the cargo output
    a developer machine has) → ``PATH`` (a Docker image compiles the ledger into
    ``/usr/local/bin``) → the committed ``bin/torchain`` (the prebuilt Linux
    binary shipped for hosts without a Rust toolchain, and therefore the last
    resort — it can be older than the seed it must read). Reading
    ``settings.chain_binary`` directly instead is what made signup answer ``503
    CHAIN_UNAVAILABLE`` on every node that never ran ``cargo build`` — the wallet
    code now asks the same question the start command asks, so both get the same
    answer.
    """
    from core import ledger_host      # late: keep the import graph shallow

    binary = ledger_host.find_binary()
    if not binary:
        raise ChainUnavailable(
            "the torchain binary is missing — build it with `cargo build "
            "--release` in rust/torchain, ship bin/torchain, or set "
            "ONENESS_NODE_BINARY",
            details={"chain.binary": str(settings.chain_binary),
                     "looked_in": [str(settings.chain_binary),
                                   "PATH (torchain)", "bin/torchain"]})
    return _runnable(binary)


def new_wallet(name: str, words: int = 24) -> dict:
    """Generate a Phantom-compatible wallet through the Rust binary."""
    import subprocess

    safe = str(name or "").strip()
    if not WALLET_NAME.match(safe):
        raise ValidationError("wallet name must match " + WALLET_NAME.pattern)
    target = wallet_dir() / f"{safe}.json"
    if target.exists():
        raise ValidationError(f"wallet {safe!r} already exists",
                              details={"path": str(target)})
    binary = binary_path()
    result = subprocess.run(
        [binary, "wallet", "new", "--out", str(target),
         "--words", "12" if int(words) == 12 else "24"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    if result.returncode != 0 or not target.exists():
        raise ChainUnavailable(
            f"wallet generation failed: {(result.stderr or result.stdout)[:400]}")
    wallet = json.loads(target.read_text(encoding="utf-8"))
    return {"name": safe, "address": wallet.get("address"),
            "pubkey_base58": wallet.get("pubkey_base58"),
            "mnemonic": wallet.get("mnemonic"), "path": str(target)}


def sign_only(name: str, *, kind: str, to: str, amount: float,
              token: str | None = None, note: str = "",
              privacy: object = None) -> dict:
    """Build and sign without broadcasting (the node submits it later).

    ``privacy`` rides in ``external_ref``, so the mode survives the hand-off:
    posting this ``signed_tx`` to ``/api/chain/tx`` lands the same record a
    direct send would have landed.
    """
    mode = privacy_mode(privacy)
    wallet = load_wallet(name)
    validate_address(to, "to")
    amount_value = int(amount) if token else to_micro(float(amount))
    signed = sign_transaction(wallet, {
        "kind": kind, "from": wallet["address"], "to": to,
        "amount": amount_value, "token": token or None, "note": note,
        "external_ref": chain_ref(mode),
        "nonce": int(node_account(wallet["address"]).get("nonce") or 0),
        "timestamp": int(time.time())})
    return {"signed_tx": signed,
            "message": "POST this to /api/chain/tx to broadcast",
            "amount_value": amount_value,
            "privacy": mode,
            "on_chain_from": (anonymous_label() if mode != PUBLIC
                              else str(wallet["address"])),
            "unit": "token" if token else "microTOR",
            "micro_per_tor": MICRO_PER_TOR}


# ------------------------------------------------------- import / export keys
#: BIP39 + SLIP-0010 path the Rust wallet and Phantom both use.
DERIVATION_PATH = "m/44'/501'/0'/0'"
_HARDENED = 0x8000_0000


def _slip10_ed25519(seed: bytes, path: str = DERIVATION_PATH) -> bytes:
    """SLIP-0010 ed25519 derivation (all indexes hardened) — 32-byte seed."""
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for chunk in str(path).split("/")[1:]:
        index = int(chunk.rstrip("'")) | _HARDENED
        digest = hmac.new(chain, b"\x00" + key + index.to_bytes(4, "big"),
                          hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def seed_from_mnemonic(phrase: str) -> bytes:
    """BIP39 seed (PBKDF2-HMAC-SHA512, 2048 rounds, empty passphrase)."""
    words = str(phrase or "").split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise ValidationError(
            "mnemonic must be 12, 15, 18, 21 or 24 words",
            details={"words": len(words)})
    return hashlib.pbkdf2_hmac("sha512", " ".join(words).encode("utf-8"),
                               b"mnemonic", 2048, dklen=64)


def _wallet_from_secret(secret_b58: str) -> dict:
    """Derive the keystore fields from a Phantom private key (seed‖pubkey)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    text = str(secret_b58 or "").strip()
    try:
        raw = base58.b58decode(text)
    except Exception:  # noqa: BLE001 - a bad key is a validation error, not a 500
        raise ValidationError("private key is not valid base58")
    if len(raw) not in (32, 64):
        raise ValidationError(
            "private key must decode to 32 or 64 bytes (Phantom exports 64)",
            details={"bytes": len(raw)})
    seed = raw[:32]
    private = Ed25519PrivateKey.from_private_bytes(seed)
    pubkey = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if len(raw) == 64 and raw[32:] != pubkey:
        raise ValidationError(
            "private key tail does not match its public key — the Phantom key "
            "is corrupt or truncated")
    from oneness_tor.services import wallet_access

    return {"address": wallet_access.address_for_pubkey(pubkey),
            "pubkey_base58": base58.b58encode(pubkey).decode("ascii"),
            "secret_key_base58": base58.b58encode(seed + pubkey).decode("ascii"),
            "seed": seed}


def _wallet_from_mnemonic(phrase: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_bytes = _slip10_ed25519(seed_from_mnemonic(phrase))
    private = Ed25519PrivateKey.from_private_bytes(private_bytes)
    pubkey = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    from oneness_tor.services import wallet_access

    return {"address": wallet_access.address_for_pubkey(pubkey),
            "pubkey_base58": base58.b58encode(pubkey).decode("ascii"),
            "secret_key_base58": base58.b58encode(private_bytes + pubkey).decode("ascii"),
            "seed": private_bytes}


def _mnemonic_via_binary(phrase: str) -> dict | None:
    """Let the Rust node validate the BIP39 checksum and derive the key.

    ``torchain wallet from-mnemonic`` uses the ``bip39`` crate, so a typo in one
    word is caught instead of silently deriving a different wallet. Returns
    ``None`` when the binary is unavailable (the pure-Python SLIP-0010 path then
    derives the same key, and the API tells the caller to verify the address).
    """
    import subprocess

    from core import ledger_host

    found = ledger_host.find_binary()
    if not found:
        return None
    binary = _runnable(found)
    try:
        result = subprocess.run(
            [binary, "wallet", "from-mnemonic", "--mnemonic", phrase],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:240]
        raise ValidationError(f"invalid recovery phrase: {detail or 'rejected'}")
    secret = ""
    for line in (result.stdout or "").splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower().replace(" ", "_") in ("secret_b58", "secret"):
            secret = value.strip()
    if not secret:
        return None
    return _wallet_from_secret(secret)


def import_wallet(name: str, *, secret_key_base58: object = None,
                  mnemonic: object = None) -> dict:
    """Write a keystore from an existing key/mnemonic (no chain call needed).

    The address is derived exactly like the Rust node derives it, so the
    imported wallet can sign real transactions immediately. ``mnemonic`` is
    derived with BIP39 + SLIP-0010 (``m/44'/501'/0'/0'``); the caller should
    compare the returned address with the wallet it exported from.
    """
    safe = str(name or "").strip()
    if not WALLET_NAME.match(safe):
        raise ValidationError("wallet name must match " + WALLET_NAME.pattern)
    target = wallet_dir() / f"{safe}.json"
    if target.exists():
        raise ValidationError(f"wallet {safe!r} already exists",
                              details={"path": str(target)})
    phrase = str(mnemonic or "").strip()
    source = "mnemonic" if phrase else "private_key"
    if phrase:
        derived = _mnemonic_via_binary(phrase) or _wallet_from_mnemonic(phrase)
    else:
        derived = _wallet_from_secret(str(secret_key_base58 or ""))
    wallet = {
        "version": 1, "kind": "oneness-tor-keystore", "algorithm": "ed25519",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "address": derived["address"], "pubkey_base58": derived["pubkey_base58"],
        "secret_key_base58": derived["secret_key_base58"],
        "mnemonic": phrase or None, "imported": True, "imported_from": source,
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(wallet, indent=2), encoding="utf-8")
    temporary.replace(target)
    if os.name != "nt":
        try:
            target.chmod(0o600)
        except OSError:
            pass
    return {"name": safe, "address": wallet["address"],
            "pubkey_base58": wallet["pubkey_base58"],
            "imported_from": source, "path": str(target)}


def export_wallet(name: str) -> dict:
    """The keystore's secret material — callers MUST verify the password first.

    This function never checks authorization on its own: it is the last step of
    an already-authenticated export (account password + wallet ownership).
    """
    wallet = load_wallet(name)
    secret = str(wallet.get("secret_key_base58") or "")
    if not secret:
        raise ValidationError(f"wallet {name!r} has no private key in its keystore",
                              details={"wallet": str(name)})
    return {"name": str(name), "address": wallet.get("address"),
            "pubkey_base58": wallet.get("pubkey_base58"),
            "secret_key_base58": secret,
            "mnemonic": wallet.get("mnemonic"),
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "standard": "oneness-tor-keystore/1",
            "warning": ("anyone who has this key controls the wallet and every "
                        "balance in it. Store it offline; never share it.")}


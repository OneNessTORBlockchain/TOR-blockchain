"""Founder wallet + derivation invariants (pure Python, no network).

The founder key must stay Phantom-compatible: BIP39 mnemonic -> SLIP-0010
``m/44'/501'/0'/0'`` -> Ed25519, secret = seed||pubkey (64 bytes) base58.
Addresses must satisfy the LOCKED chain regex from ``core/validator.py``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import base58
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.validator import MICRO_PER_TOR, NATIVE_COIN_ID, is_address, validate_address  # noqa: E402
from tools.gen_founder_wallet import (  # noqa: E402
    GENESIS_SUPPLY_TOR,
    build_wallet,
    slip10_ed25519,
)
from tools.torchain_signer import canonical_message, sign_transaction  # noqa: E402

MNEMONIC = ("abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon about")


def test_slip10_matches_the_official_ed25519_vector():
    key = slip10_ed25519(bytes.fromhex("000102030405060708090a0b0c0d0e0f"), "m/0'")
    assert key.hex() == ("68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e"
                         "704f1dade7a3")


def test_wallet_is_deterministic_and_phantom_shaped():
    first = build_wallet(MNEMONIC)
    second = build_wallet(MNEMONIC)
    assert first["address"] == second["address"]
    assert first["secret_key_base58"] == second["secret_key_base58"]
    secret = base58.b58decode(first["secret_key_base58"])
    assert len(secret) == 64, "Phantom imports seed||pubkey"
    key = Ed25519PrivateKey.from_private_bytes(secret[:32])
    assert secret[32:] == key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def test_address_is_sha3_base32_of_the_pubkey_and_matches_the_locked_regex():
    wallet = build_wallet(MNEMONIC)
    pubkey = base58.b58decode(wallet["pubkey_base58"])
    digest = hashlib.sha3_256(pubkey).digest()[:20]
    body = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    assert wallet["address"] == "TOR1" + body + "onion"
    assert is_address(wallet["address"])
    assert wallet["address"] != NATIVE_COIN_ID
    validate_address(wallet["address"])


def test_signature_roundtrip_over_the_chain_message():
    wallet = build_wallet(MNEMONIC)
    signed = sign_transaction(wallet, {
        "kind": "transfer", "from": wallet["address"], "to": wallet["address"],
        "amount": 150_000_000, "token": None, "note": " hi ",
        "nonce": 0, "timestamp": 1_700_000_000,
    })
    message = canonical_message(signed)
    assert message == (b"oneness-tor:v1|transfer|" + wallet["address"].encode()
                       + b"|" + wallet["address"].encode()
                       + b"|150000000|TOR|hi|0|1700000000")
    key = Ed25519PrivateKey.from_private_bytes(
        base58.b58decode(wallet["secret_key_base58"])[:32])
    key.public_key().verify(base58.b58decode(signed["signature"]), message)


def test_saved_founder_wallet_and_genesis_allocation_agree():
    wallet_path = ROOT / "data" / "wallets" / "founder.json"
    allocation_path = ROOT / "data" / "genesis" / "founder_allocation.json"
    if not wallet_path.exists() or not allocation_path.exists():
        pytest.skip("founder wallet not generated yet")
    wallet = json.loads(wallet_path.read_text(encoding="utf-8"))
    rebuilt = build_wallet(wallet["mnemonic"])
    assert rebuilt["address"] == wallet["address"]
    assert rebuilt["secret_key_base58"] == wallet["secret_key_base58"]
    allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
    assert allocation["founder_address"] == wallet["address"]
    assert allocation["supply_tor"] == GENESIS_SUPPLY_TOR
    assert allocation["supply_micro"] == GENESIS_SUPPLY_TOR * MICRO_PER_TOR

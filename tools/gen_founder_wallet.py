"""Generate the OneNess TOR founder wallet — Ed25519, Phantom-compatible.

Phantom stores Ed25519 keys as a **64-byte secret key = seed || public key**,
base58-encoded, derived from a BIP39 mnemonic on the Solana path
``m/44'/501'/0'/0'`` (SLIP-0010 hardened Ed25519). This tool produces exactly
that, so the same mnemonic or the same base58 secret key imports into Phantom.

On-chain addresses stay in the chain's own format (``TOR1…onion``): the address
is ``base32(sha3-256(pubkey)[:20])``, so transfers stay verifiable with Ed25519.

Usage
-----
    python tools/gen_founder_wallet.py                    # generate + save
    python tools/gen_founder_wallet.py --show-only        # reprint saved keys
    python tools/gen_founder_wallet.py --mnemonic "..."   # restore determinism
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import base58
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mnemonic import Mnemonic

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.validator import validate_address  # noqa: E402  (path set above)

DERIVATION_PATH = "m/44'/501'/0'/0'"
HARDENED = 0x80000000
GENESIS_SUPPLY_TOR = 50_000_000
MICRO_PER_TOR = 10 ** 8
NATIVE_COIN_ID = "TOR111111111111111111111111111111onion"


def slip10_ed25519(seed: bytes, path: str = DERIVATION_PATH) -> bytes:
    """SLIP-0010 Ed25519 derivation -> 32-byte private key."""
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for chunk in path.split("/")[1:]:
        index = int(chunk.rstrip("'")) + HARDENED
        data = b"\x00" + key + index.to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def address_for_pubkey(pubkey: bytes) -> str:
    """``TOR1`` + base32(sha3-256(pubkey)[:20]) + ``onion``."""
    digest = hashlib.sha3_256(pubkey).digest()[:20]
    body = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return "TOR1" + body + "onion"


def build_wallet(mnemonic_phrase: str) -> dict:
    # BIP39 seed (NFKD not required for English wordlists)
    seed = hashlib.pbkdf2_hmac("sha512", mnemonic_phrase.encode("utf-8"),
                               b"mnemonic", 2048)
    private_seed = slip10_ed25519(seed)
    signing_key = Ed25519PrivateKey.from_private_bytes(private_seed)
    pubkey = signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    secret_b58 = base58.b58encode(private_seed + pubkey).decode("ascii")
    return {
        "version": 1,
        "kind": "oneness-tor-founder",
        "algorithm": "ed25519",
        "wallet_standard": "phantom-compatible (BIP39 + SLIP-0010 ed25519)",
        "derivation_path": DERIVATION_PATH,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "address": address_for_pubkey(pubkey),
        "pubkey_base58": base58.b58encode(pubkey).decode("ascii"),
        "pubkey_hex": pubkey.hex(),
        "secret_key_base58": secret_b58,
        "mnemonic": mnemonic_phrase,
        "notes": ("secret_key_base58 is seed||pubkey (64 bytes) exactly as Phantom "
                  "imports it. Never commit this file."),
    }


def self_check(wallet: dict) -> None:
    """Round-trip: rebuild from the mnemonic and verify a real signature."""
    rebuilt = build_wallet(wallet["mnemonic"])
    assert rebuilt["pubkey_base58"] == wallet["pubkey_base58"], "pubkey mismatch"
    assert rebuilt["secret_key_base58"] == wallet["secret_key_base58"], "secret mismatch"
    secret = base58.b58decode(wallet["secret_key_base58"])
    assert len(secret) == 64, "Phantom secret keys are 64 bytes"
    key = Ed25519PrivateKey.from_private_bytes(secret[:32])
    signature = key.sign(b"oneness-tor-founder-check")
    key.public_key().verify(signature, b"oneness-tor-founder-check")
    assert secret[32:] == key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    validate_address(wallet["address"])  # locked regex from core.validator


def main() -> int:
    parser = argparse.ArgumentParser(description="OneNess TOR founder wallet")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--mnemonic", default=None, help="restore from a phrase")
    parser.add_argument("--words", type=int, default=24, choices=(12, 15, 18, 21, 24))
    parser.add_argument("--show-only", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="hide the secret key")
    args = parser.parse_args()

    wallet_path = Path(args.data_dir) / "wallets" / "founder.json"
    wallet_path.parent.mkdir(parents=True, exist_ok=True)
    public_path = wallet_path.with_name("founder.public.json")

    if args.show_only:
        if not wallet_path.exists():
            print(f"no founder wallet at {wallet_path}")
            return 1
        wallet = json.loads(wallet_path.read_text(encoding="utf-8"))
    else:
        if wallet_path.exists() and not args.mnemonic:
            print(f"refusing to overwrite existing wallet: {wallet_path}")
            print("use --show-only to read it, or move it aside first.")
            return 1
        phrase = args.mnemonic or Mnemonic("english").generate(
            strength=args.words * 32 // 3)
        wallet = build_wallet(phrase)
        self_check(wallet)
        wallet_path.write_text(json.dumps(wallet, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(wallet_path, 0o600)
        except OSError:
            pass
        public = {k: v for k, v in wallet.items()
                  if k not in ("secret_key_base58", "mnemonic")}
        public_path.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")

        genesis_dir = Path(args.data_dir) / "genesis"
        genesis_dir.mkdir(parents=True, exist_ok=True)
        (genesis_dir / "founder_allocation.json").write_text(
            json.dumps({
                "chain_id": "072316",
                "native_coin_id": NATIVE_COIN_ID,
                "founder_address": wallet["address"],
                "founder_pubkey_base58": wallet["pubkey_base58"],
                "supply_tor": GENESIS_SUPPLY_TOR,
                "supply_micro": GENESIS_SUPPLY_TOR * MICRO_PER_TOR,
                "rule": "block 0 allocates the entire 50,000,000 TOR supply to this wallet",
            }, indent=2) + "\n", encoding="utf-8")

    print(f"wallet file : {wallet_path}")
    print(f"address     : {wallet['address']}")
    print(f"pubkey b58  : {wallet['pubkey_base58']}")
    if args.quiet:
        print("secret key  : (hidden — use --show-only)")
    else:
        print(f"secret b58  : {wallet['secret_key_base58']}")
        print(f"mnemonic    : {wallet['mnemonic']}")
    print(f"genesis     : {GENESIS_SUPPLY_TOR:,} TOR minted to this wallet at block 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

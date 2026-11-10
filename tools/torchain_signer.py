"""Sign OneNess TOR transactions from Python.

The Rust node verifies exactly these bytes:

    oneness-tor:v1|<kind>|<from>|<to>|<amount>|<token or TOR>|<note>|<nonce>|<timestamp>

`from` is the signer's ``TOR1…onion`` address, ``<amount>`` is microTOR for the
native coin and whole units for a token, and the signature is Ed25519 over that
string, base58-encoded.

CLI examples
------------
    python tools/torchain_signer.py --wallet data/wallets/founder.json \
        --kind transfer --to TOR1…onion --amount 1.5 --node http://127.0.0.1:8545
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import base58
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.validator import MICRO_PER_TOR, validate_address  # noqa: E402

MESSAGE_PREFIX = "oneness-tor:v1"


def canonical_message(tx: dict) -> bytes:
    """Byte-for-byte the string ``chain.rs`` rebuilds before verifying."""
    token = (tx.get("token") or "TOR") or "TOR"
    note = (tx.get("note") or "").strip()
    return (
        f"{MESSAGE_PREFIX}|{tx['kind']}|{tx['from']}|{tx.get('to', '')}"
        f"|{int(tx['amount'])}|{token}|{note}|{int(tx['nonce'])}|{int(tx['timestamp'])}"
    ).encode("utf-8")


def sign_transaction(wallet: dict, tx: dict) -> dict:
    """Attach ``pubkey_base58`` + ``signature`` to ``tx`` (raises on bad wallet)."""
    secret = base58.b58decode(wallet["secret_key_base58"])
    if len(secret) != 64:
        raise ValueError("wallet secret_key_base58 must be the 64-byte Phantom key")
    key = Ed25519PrivateKey.from_private_bytes(secret[:32])
    payload = {
        "kind": tx["kind"],
        "from": tx["from"],
        "to": tx.get("to", ""),
        "amount": int(tx["amount"]),
        "token": tx.get("token"),
        "note": tx.get("note", ""),
        "external_ref": tx.get("external_ref", ""),
        "nonce": int(tx["nonce"]),
        "timestamp": int(tx["timestamp"]),
        "pubkey_base58": wallet["pubkey_base58"],
        "signature": "",
    }
    payload["signature"] = base58.b58encode(key.sign(canonical_message(payload))).decode()
    return payload


def http_json(url: str, payload: dict | None = None, token: str | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"}
    if token:
        headers["x-admin-token"] = token
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="GET" if payload is None else "POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": {"code": f"HTTP_{error.code}", "message": body}}


def next_nonce(node: str, address: str) -> int:
    reply = http_json(f"{node.rstrip('/')}/api/account/{address}")
    if reply.get("ok"):
        return int(reply["data"].get("nonce", 0))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign (and optionally submit) a TOR transaction")
    parser.add_argument("--wallet", default=str(ROOT / "data" / "wallets" / "founder.json"))
    parser.add_argument("--kind", default="transfer",
                        choices=("transfer", "nft_transfer", "deposit", "withdraw",
                                 "token_create", "pool_create", "swap", "mint"))
    parser.add_argument("--to", default="")
    parser.add_argument("--amount", default="0")
    parser.add_argument("--token", default=None)
    parser.add_argument("--note", default="")
    parser.add_argument("--ref", default="", help="external deposit/withdraw reference")
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--unit", default="auto", choices=("auto", "tor", "token"))
    parser.add_argument("--node", default=None, help="POST to this node, e.g. http://127.0.0.1:8545")
    parser.add_argument("--dry-run", action="store_true", help="print the signed JSON only")
    args = parser.parse_args()

    wallet = json.loads(Path(args.wallet).read_text(encoding="utf-8"))
    unit = args.unit
    if unit == "auto":
        unit = "token" if args.token or args.kind in ("token_create", "mint", "pool_create") else "tor"
    if unit == "tor":
        amount = int((Decimal(str(args.amount)) * MICRO_PER_TOR).to_integral_value())
    else:
        amount = int(args.amount)
    if args.to:
        validate_address(args.to)
    if args.kind in ("deposit", "withdraw"):
        # these are admin-only on the node; failing early is friendlier
        pass

    nonce = int(args.nonce) if args.nonce is not None else (
        next_nonce(args.node, wallet["address"]) if args.node else 0)
    tx = {
        "kind": args.kind,
        "from": wallet["address"],
        "to": args.to,
        "amount": amount,
        "token": args.token,
        "note": args.note,
        "external_ref": args.ref,
        "nonce": nonce,
        "timestamp": int(time.time()),
    }
    signed = sign_transaction(wallet, tx)
    if args.node and not args.dry_run:
        print(json.dumps(http_json(f"{args.node.rstrip('/')}/api/chain/tx", signed), indent=2))
        return 0
    print(json.dumps(signed, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

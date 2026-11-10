"""Prove, on a running node, that PUBLIC and ANONYMOUS sends are both permanent.

What this does, in order, against ``--node`` (a live node, by default the public
origin). Nothing here is simulated:

1. makes (or reuses) a **recipient wallet** in ``data/wallets/`` so the two test
   transfers have a second party to land on — the address rule is the chain's own
   (``TOR1`` + base32(sha3-256(pubkey)[:20]) + ``onion``, ``rust/torchain/src/util.rs``);
2. signs a **PUBLIC** transfer from the founder wallet: ``external_ref`` is empty,
   so the record is byte-identical to a legacy transfer and the sender's address is
   on chain forever;
3. signs an **ANONYMOUS** transfer: ``external_ref`` carries
   ``oneness-privacy:1|mode=anonymous`` (``oneness_tor/services/privacy.py``),
   which is the only thing that has to survive a node restart;
4. reads both records back through the three public projections the privacy rule
   masks — ``GET /api/chain/tx/<txid>``, ``GET /api/public/records`` and
   ``GET /api/chain/events`` — and asserts the anonymous one shows
   ``Tor-Anonymous-onion`` while the public one still shows the founder address.

Anything that cannot be proven is reported as FAIL, never assumed.

Usage
-----
    python tools/live_privacy_proof.py                          # live origin
    python tools/live_privacy_proof.py --node http://127.0.0.1:8090
    python tools/live_privacy_proof.py --amount 0.50 --dry-run  # sign, do not send

Exit codes: 0 = every check passed, 1 = a check failed, 2 = precondition failed
(no node, no founder wallet, node refuses the write).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from hashlib import sha3_256
from pathlib import Path

import base58

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "tools")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from core.validator import MICRO_PER_TOR, validate_address  # noqa: E402
from torchain_signer import http_json, sign_transaction  # noqa: E402

ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
DEFAULT_NODE = "https://onenesstorchain.onrender.com"


def _privacy_rule():
    """The wallet's own privacy rule, loaded by path.

    Copying the constants here would let the tool and the API drift apart, so the
    tool executes ``oneness_tor/services/privacy.py`` itself instead.
    """
    path = ROOT / "oneness_tor" / "services" / "privacy.py"
    spec = importlib.util.spec_from_file_location("oneness_privacy_rule", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


privacy = _privacy_rule()


class Report:
    """A PASS/FAIL list that prints as it goes and can be written to JSON."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def check(self, name: str, passed: bool, **detail) -> bool:
        self.steps.append({"step": name, "ok": bool(passed), **detail})
        mark = "PASS" if passed else "FAIL"
        tail = f" -> {json.dumps(detail, default=str)[:400]}" if detail else ""
        print(f"[{mark}] {name}{tail}")
        return bool(passed)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.get("ok") for step in self.steps)



def base32_lower(data: bytes) -> str:
    """Lowercase RFC 4648 base32 without padding — the address body alphabet."""
    out: list[str] = []
    buffer = 0
    bits = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(ALPHABET[(buffer >> bits) & 0x1F])
    if bits:
        out.append(ALPHABET[(buffer << (5 - bits)) & 0x1F])
    return "".join(out)


def address_from_pubkey(pubkey: bytes) -> str:
    """``TOR1`` + base32(sha3-256(pubkey)[..20]) + ``onion`` — identical to the node."""
    return "TOR1" + base32_lower(sha3_256(pubkey).digest()[:20]) + "onion"


def recipient_wallet(path: Path) -> dict:
    """A second wallet for the transfers to land on (generated once, then reused)."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    key = Ed25519PrivateKey.generate()
    secret = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pubkey = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    wallet = {
        "address": address_from_pubkey(pubkey),
        "pubkey_base58": base58.b58encode(pubkey).decode(),
        "secret_key_base58": base58.b58encode(secret + pubkey).decode(),
        "note": "made by tools/live_privacy_proof.py — a recipient, not the founder",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wallet, indent=2), encoding="utf-8")
    return wallet


def account(node: str, address: str) -> dict:
    reply = http_json(f"{node}/api/account/{address}")
    return (reply.get("data") or {}) if reply.get("ok") else {}


def send(node: str, wallet: dict, *, to: str, amount_tor: float, note: str,
         mode: str, dry_run: bool) -> tuple[dict, dict]:
    """Sign one transfer with the wallet's privacy answer, then submit it."""
    before = account(node, wallet["address"])
    tx = {
        "kind": "transfer",
        "from": wallet["address"],
        "to": to,
        "amount": int(round(amount_tor * MICRO_PER_TOR)),
        "token": None,
        "note": note,
        "external_ref": privacy.chain_ref(mode),
        "nonce": int(before.get("nonce") or 0),
        "timestamp": int(time.time()),
    }
    signed = sign_transaction(wallet, tx)
    if dry_run:
        return signed, {}
    return signed, http_json(f"{node}/api/chain/tx", signed)


def records_from(reply: dict) -> list[dict]:
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        return []
    rows = data.get("records")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def txid_of(rows: list[dict]) -> str:
    for row in rows:
        value = str(row.get("txid") or "")
        if value:
            return value
    return ""


def find_row(rows: list[dict], txid: str) -> dict:
    for row in rows:
        if str(row.get("txid") or "") == txid:
            return row
    return {}


def masked(row: dict) -> bool:
    """The anonymous projection: the label, the flag, and ``from`` in masked_fields."""
    return (str(row.get("from") or "") == privacy.label()
            and bool(row.get("anonymous"))
            and "from" in (row.get("masked_fields") or []))


def open_row(row: dict, sender: str) -> bool:
    """The public projection: the sender's own address, nothing hidden."""
    return (str(row.get("from") or "") == sender
            and not row.get("anonymous")
            and not (row.get("masked_fields") or []))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove PUBLIC + ANONYMOUS sends on a running node")
    parser.add_argument("--node", default=DEFAULT_NODE, help="node base URL")
    parser.add_argument("--amount", type=float, default=1.00,
                        help="TOR per test transfer")
    parser.add_argument("--wallet", default=str(ROOT / "data" / "wallets" / "founder.json"),
                        help="the sending wallet (must hold TOR)")
    parser.add_argument("--recipient",
                        default=str(ROOT / "data" / "wallets" / "privacy-recipient.json"),
                        help="where the recipient wallet is kept / created")
    parser.add_argument("--report", default="",
                        help="write the JSON report here (default logs/privacy-proof-<utc>.json)")
    parser.add_argument("--dry-run", action="store_true", help="sign only, submit nothing")
    args = parser.parse_args()

    node = args.node.rstrip("/")
    report = Report()
    print(f"node     : {node}")
    print(f"standard : {privacy.STANDARD}")
    print(f"label    : {privacy.label()}")
    print(f"anon ref : {privacy.chain_ref(privacy.ANONYMOUS)!r}")
    print(f"public ref: {privacy.chain_ref(privacy.PUBLIC)!r}\n")

    wallet_path = Path(args.wallet)
    if not wallet_path.exists():
        print(f"no wallet at {wallet_path}", file=sys.stderr)
        return 2
    founder = json.loads(wallet_path.read_text(encoding="utf-8"))
    recipient = recipient_wallet(Path(args.recipient))
    validate_address(recipient["address"], "recipient")

    report.check("recipient address follows the chain rule",
                 recipient["address"] == address_from_pubkey(
                     base58.b58decode(recipient["pubkey_base58"])),
                 address=recipient["address"])

    info = http_json(f"{node}/api/chain/info")
    if not info.get("ok"):
        print(f"node is not answering /api/chain/info: {info.get('error')}", file=sys.stderr)
        return 2
    height_before = int((info.get("data") or {}).get("height") or 0)
    sender = account(node, founder["address"])
    report.check("founder wallet holds TOR on this node",
                 float(sender.get("balance") or 0) >= args.amount * 2,
                 balance_tor=sender.get("balance"), nonce=sender.get("nonce"),
                 height=height_before)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    notes = {
        privacy.PUBLIC: f"PUBLIC send {stamp}: the sender's address stays on this record",
        privacy.ANONYMOUS: f"ANONYMOUS send {stamp}: the sender shows as {privacy.label()}",
    }

    sent: dict[str, dict] = {}
    for mode in (privacy.PUBLIC, privacy.ANONYMOUS):
        signed, reply = send(node, founder, to=recipient["address"],
                             amount_tor=args.amount, note=notes[mode], mode=mode,
                             dry_run=args.dry_run)
        rows = records_from(reply)
        sent[mode] = {"signed": signed, "reply": reply, "txid": txid_of(rows),
                      "record": rows[0] if rows else {}}
        if args.dry_run:
            print(json.dumps(signed, indent=2))
            continue
        report.check(f"{mode.upper()} transfer accepted by the node",
                     bool(reply.get("ok")) and bool(sent[mode]["txid"]),
                     txid=sent[mode]["txid"],
                     height=(reply.get("data") or {}).get("height"),
                     external_ref=signed["external_ref"],
                     error=(reply.get("error") or {}).get("code"))
        # the second transfer must not reuse the first one's nonce
        time.sleep(1.5)

    if args.dry_run:
        print("\n=== RESULT:", "PASS" if report.ok else "FAIL", "(dry run) ===")
        return 0 if report.ok else 1

    # ---- read both records back through every public projection --------------
    public_txid = sent[privacy.PUBLIC]["txid"]
    anon_txid = sent[privacy.ANONYMOUS]["txid"]
    read = {
        "chain_tx": {
            mode: (http_json(f"{node}/api/chain/tx/{sent[mode]['txid']}").get("data") or {})
            for mode in (privacy.PUBLIC, privacy.ANONYMOUS) if sent[mode]["txid"]
        },
        "records": (http_json(f"{node}/api/public/records?limit=12").get("data") or {}),
        "events": (http_json(f"{node}/api/chain/events?limit=12").get("data") or {}),
    }
    record_list = read["records"].get("records") or []
    event_list = read["events"].get("events") or []
    by_txid = read["chain_tx"]

    for mode in (privacy.PUBLIC, privacy.ANONYMOUS):
        row = by_txid.get(mode) or {}
        print(f"/api/chain/tx [{mode}] from={row.get('from')!r} "
              f"privacy={row.get('privacy')!r} masked={row.get('masked_fields')}")

    report.check("PUBLIC record keeps the sender's address on /api/chain/tx",
                 open_row(by_txid.get(privacy.PUBLIC) or {}, founder["address"]),
                 txid=public_txid)
    report.check("ANONYMOUS record shows the label on /api/chain/tx",
                 masked(by_txid.get(privacy.ANONYMOUS) or {}), txid=anon_txid,
                 seen=(by_txid.get(privacy.ANONYMOUS) or {}).get("from"))

    for name, rows in (("/api/public/records", record_list),
                       ("/api/chain/events", event_list)):
        public_row = find_row(rows, public_txid)
        anon_row = find_row(rows, anon_txid)
        report.check(f"{name} lists the PUBLIC row unmasked",
                     bool(public_row) and open_row(public_row, founder["address"]),
                     txid=public_txid)
        report.check(f"{name} lists the ANONYMOUS row masked",
                     bool(anon_row) and masked(anon_row), txid=anon_txid,
                     seen=(anon_row or {}).get("from"))

    after = http_json(f"{node}/api/chain/info").get("data") or {}
    height_after = int(after.get("height") or 0)
    landed = {mode: int((sent[mode]["reply"].get("data") or {}).get("height") or 0)
              for mode in sent}
    report.check("both transactions sit in blocks above the height we started at",
                 all(h > height_before for h in landed.values())
                 and height_after > height_before,
                 height_before=height_before, height_after=height_after,
                 public_height=landed[privacy.PUBLIC],
                 anonymous_height=landed[privacy.ANONYMOUS])

    print(f"\nfounder balance now: {account(node, founder['address']).get('balance')} TOR")
    print(f"head hash now      : {after.get('head_hash')}")

    payload = {
        "ok": report.ok,
        "node": node,
        "standard": privacy.STANDARD,
        "anonymous_label": privacy.label(),
        "anonymous_ref": privacy.chain_ref(privacy.ANONYMOUS),
        "founder": founder["address"],
        "recipient": recipient["address"],
        "amount_tor": args.amount,
        "height_before": height_before,
        "height_after": height_after,
        "head_hash": after.get("head_hash"),
        "transactions": {
            mode: {"txid": sent[mode]["txid"], "height": landed.get(mode),
                   "external_ref": sent[mode]["signed"]["external_ref"],
                   "note": sent[mode]["signed"]["note"],
                   "read_back": by_txid.get(mode, {})}
            for mode in sent
        },
        "steps": report.steps,
    }
    target = Path(args.report) if args.report else ROOT / "logs" / f"privacy-proof-{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nreport written to {target}")
    print("=== RESULT:", "PASS" if report.ok else "FAIL", "===")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

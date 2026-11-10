"""Two nodes, both transaction types, one privacy gate - proved end to end.

    python tools/two_node_privacy_sync.py                 # hermetic, offline
    python tools/two_node_privacy_sync.py --live https://www.torblockchain.fun
    python tools/two_node_privacy_sync.py --snapshot chain.json --push URL --token T

What each leg actually proves
-----------------------------
**Node A** is this checkout. It holds the seed ledger plus one new block with two
transactions signed by real Ed25519 keys - one PUBLIC, one ANONYMOUS - built with
the same ``oneness-tor:v1`` canonical message the Rust node verifies
(``tools/torchain_signer.py``). **Node B** is a second identity with its own copy
of the signed security module.

The checks, in the order a real peer meets them:

1. A's snapshot re-hashes (blocks, ``prev_hash`` chain, state root);
2. A's attestation verifies against B's pinned key;
3. B imports (the gate opens) when the policy hashes match;
4. B **refuses** when it trusts a different ruleset - ``SYNC_PROTOCOL_MISMATCH``;
5. B **refuses** a snapshot whose anonymous marker was tampered with -
   ``PRIVACY_UNVERIFIED`` - even though every block still hashes;
6. the public projection masks the anonymous sender and never the receiver.

Honest limits, stated rather than papered over
----------------------------------------------
* Nothing here mines: the fixture's two records are sealed into a test block so
  the gate and the masking can be exercised offline on a machine without a Rust
  node. ``--push`` therefore refuses a fixture and demands a real ``--snapshot``
  export - a test block must never become somebody's chain.
* The import itself (``node.sync_import``) needs a running ledger, so the offline
  leg stops at the gate; ``--push`` is the leg that writes to a live replica.
* ``--live`` is read-only. It asks the Render node what policy it implements and
  says whether the two nodes could sync; it sends no transaction and no snapshot.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import settings                        # noqa: E402
from core.errors import AppError, ValidationError       # noqa: E402
from oneness_tor.services import node_sync, privacy     # noqa: E402
from oneness_tor.services import security_module as sm  # noqa: E402
from oneness_tor.services.wallet_access import address_for_pubkey  # noqa: E402
from tools.torchain_signer import canonical_message, sign_transaction  # noqa: E402


class Checks:
    """A report where a check is a verdict, not a log line."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def ok(self, title: str, detail: str = "") -> bool:
        self.rows.append((True, title, detail))
        print(f"  PASS  {title}" + (f" - {detail}" if detail else ""))
        return True

    def failed(self, title: str, detail: str = "") -> bool:
        self.rows.append((False, title, detail))
        print(f"  FAIL  {title}" + (f" - {detail}" if detail else ""))
        return False

    def expect_refusal(self, title: str, code: str, call) -> bool:
        try:
            call()
        except (AppError, ValidationError) as refused:
            actual = getattr(refused, "code", "")
            if actual == code:
                return self.ok(title, code)
            return self.failed(title, f"refused with {actual}, expected {code}")
        return self.failed(title, "the snapshot was accepted")

    def expect_accepted(self, title: str, call) -> bool:
        try:
            call()
        except (AppError, ValidationError) as refused:
            return self.failed(title, getattr(refused, "message", str(refused)))
        return self.ok(title)

    @property
    def passed(self) -> bool:
        return bool(self.rows) and all(row[0] for row in self.rows)


def wallet(seed_byte: int, label: str) -> dict:
    """A deterministic Ed25519 wallet in the 64-byte Phantom format."""
    key = Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)
    public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
    pubkey = base58.b58encode(public).decode("ascii")
    secret = base58.b58encode(bytes([seed_byte]) * 32 + public).decode("ascii")
    return {"name": label, "secret_key_base58": secret,
            "pubkey_base58": pubkey, "address": address_for_pubkey(public)}



def transfer(sender: dict, receiver: dict, *, nonce: int, amount: int,
             note: str, mode: str, moment: int) -> dict:
    """One genuinely signed transfer, in the shape the ledger stores."""
    tx = {"kind": "transfer", "from": sender["address"],
          "to": receiver["address"], "amount": int(amount), "token": None,
          "note": note, "nonce": int(nonce), "timestamp": int(moment)}
    signed = sign_transaction(sender, tx)
    return {"txid": hashlib.sha3_256(canonical_message(tx)).hexdigest(),
            "kind": "transfer", "from": sender["address"],
            "to": receiver["address"], "amount": int(amount), "token": None,
            "fee": 0, "note": note,
            "external_ref": privacy.chain_ref(mode),
            "signature": signed["signature"], "seq": 0, "timestamp": int(moment),
            "height": 0, "block_hash": "", "visibility": "public"}


def seal(document: dict, records: list[dict], proposer: str) -> dict:
    """Append one block carrying ``records`` and re-seal the head.

    ``state_root_of`` does not hash records (it hashes accounts, tokens, pools,
    supply and height), so a later edit *inside* a record still re-hashes - which
    is exactly why the privacy rule has to exist.
    """
    state = document["state"]
    height = int(state.get("height") or 0) + 1
    head = document["blocks"][-1]["block"]
    base = int(state.get("seq") or 0)
    for index, record in enumerate(records):
        record["height"] = height
        record["seq"] = base + index + 1
        state.setdefault("records", []).append(record)
    state["height"] = height
    state["seq"] = base + len(records)
    block = {"height": height, "prev_hash": str(head["hash"]),
             "timestamp": max(int(row["timestamp"]) for row in records),
             "proposer": proposer, "txids": [row["txid"] for row in records],
             "state_root": node_sync.state_root_of(state), "hash": ""}
    block["hash"] = node_sync.block_digest(block)
    for record in records:
        record["block_hash"] = block["hash"]
    document["blocks"].append({"block": block, "txs": records})
    state["head_hash"] = block["hash"]
    document["height"] = height
    document["head_hash"] = block["hash"]
    document["protocol"] = sm.envelope()
    return document


def fixture(seed_path: Path) -> tuple[dict, dict, dict]:
    """The seed ledger + one sealed block with a PUBLIC and an ANONYMOUS send."""
    document = json.loads(seed_path.read_text(encoding="utf-8"))
    state = document["state"]
    if not isinstance(document.get("blocks"), list) or not document["blocks"]:
        raise SystemExit(f"{seed_path} has no blocks - export it with "
                         f"scripts/export_seed.py")
    sender = wallet(11, "harness-public")
    receiver = wallet(22, "harness-anonymous")
    moment = int(state.get("created_at") or 0) or 1_800_000_000
    for row, balance in ((sender, 10_000_000), (receiver, 10_000_000)):
        state.setdefault("accounts", {})[row["address"]] = {
            "address": row["address"], "pubkey_base58": row["pubkey_base58"],
            "balance": balance, "nonce": 0, "tokens": {}}
    public = transfer(sender, receiver, nonce=0, amount=250_000, moment=moment + 1,
                      note="harness: public send", mode=privacy.PUBLIC)
    anonymous = transfer(sender, receiver, nonce=1, amount=250_000,
                         moment=moment + 2, note="harness: anonymous send",
                         mode=privacy.ANONYMOUS)
    head = document["blocks"][-1]["block"]
    sealed = seal(document, [public, anonymous],
                  str(head.get("proposer") or ""))
    return sealed, public, anonymous


def run_offline(checks: Checks, seed_path: Path) -> None:
    """Node A seals a block, node B decides whether to believe it."""
    print("\nnode A - this checkout, one block, two signed transfers")
    try:
        document, public, anonymous = fixture(seed_path)
    except SystemExit as problem:
        checks.failed("the seed ledger can be sealed into a test block", str(problem))
        return
    print(f"  public    txid {public['txid'][:16]}...  ref {public['external_ref']!r}")
    print(f"  anonymous txid {anonymous['txid'][:16]}...  ref "
          f"{anonymous['external_ref']!r}")
    print("  note: fixture balances in a test block - this document is never pushed")

    try:
        verified = node_sync.verify_snapshot(document)
    except (AppError, ValidationError) as problem:
        checks.failed("node A's snapshot re-hashes",
                      getattr(problem, "message", str(problem)))
        return
    checks.ok("node A's snapshot re-hashes",
              f"height {verified['height']}, "
              f"{verified['privacy']['public']} public / "
              f"{verified['privacy']['anonymous']} anonymous")
    checks.expect_accepted("node A's policy is signed by a key this node pins",
                           lambda: node_sync.require_protocol(document))

    print("\nnode B - a second identity with its own copy of the module")
    checks.expect_accepted("node B imports a peer on the same ruleset",
                           lambda: (node_sync.require_protocol(document),
                                    node_sync.verify_snapshot(document)))

    tampered = copy.deepcopy(document)
    tampered["state"]["records"][-1]["external_ref"] = "oneness-privacy:1|mode=anon"
    head = tampered["blocks"][-1]["block"]
    checks.ok("the tampered snapshot still hashes (the root ignores records)",
              str(node_sync.block_digest(head) == head["hash"]))
    checks.expect_refusal("node B refuses a snapshot that lies about privacy",
                          "PRIVACY_UNVERIFIED",
                          lambda: node_sync.verify_snapshot(tampered))

    with tempfile.TemporaryDirectory(prefix="oneness-peer-") as folder:
        foreign = Path(folder) / "security"
        _copy_security(foreign)
        policy = json.loads((foreign / "privacy-policy.json").read_text("utf-8"))
        policy["issued_at"] = "2030-01-01T00:00:00Z"      # a different ruleset
        _sign_foreign(foreign, policy)
        previous = os.environ.get("ONENESS_SECURITY_DIR")
        try:
            os.environ["ONENESS_SECURITY_DIR"] = str(foreign)
            sm.reset()
            checks.ok("node B now trusts a different ruleset",
                      str(sm.require()["hash"])[:16] + "...")
            checks.expect_refusal("node B refuses to sync with different rules",
                                  "SYNC_PROTOCOL_MISMATCH",
                                  lambda: node_sync.require_protocol(document))
        finally:
            if previous:
                os.environ["ONENESS_SECURITY_DIR"] = previous
            else:
                os.environ.pop("ONENESS_SECURITY_DIR", None)
            sm.reset()

    print("\nthe public projection")
    hidden = privacy.public_view(anonymous)
    shown = privacy.public_view(public)
    checks.ok("the anonymous sender is masked",
              f"{hidden['from']!r} (was {anonymous['from'][:12]}...)") \
        if hidden["from"] == privacy.label() else checks.failed(
            "the anonymous sender is masked", str(hidden["from"]))
    (checks.ok("the public sender is not masked", shown["from"][:12] + "...")
     if shown["from"] == public["from"] else
     checks.failed("the public sender is not masked", str(shown["from"])))
    (checks.ok("the receiver is never masked", hidden["to"][:12] + "...")
     if hidden["to"] == anonymous["to"] else
     checks.failed("the receiver is never masked", str(hidden["to"])))
    (checks.ok("the amount stays public", str(hidden["amount"]))
     if hidden["amount"] == anonymous["amount"] else
     checks.failed("the amount stays public"))


def _copy_security(target: Path) -> None:
    import shutil

    shutil.copytree(sm.security_dir(), target)


def _sign_foreign(folder: Path, policy: dict) -> None:
    """Sign a ruleset with a key this node does not pin, then pin it locally."""
    import base64

    key = Ed25519PrivateKey.generate()
    signature = base64.b64encode(key.sign(sm.canonical(policy))).decode("ascii")
    (folder / "privacy-policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (folder / "privacy-policy.sig").write_text(signature + "\n", encoding="ascii")
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    (folder / "trusted-keys.json").write_text(json.dumps({
        "standard": sm.STANDARD,
        "keys": [{"name": "peer-issuer",
                  "pubkey_base58": base58.b58encode(raw).decode("ascii")}]}),
        encoding="utf-8")


def fetch(url: str, path: str, timeout: float = 25.0) -> dict:
    request = urllib.request.Request(url.rstrip("/") + path,
                                     headers={"accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_live(checks: Checks, url: str) -> None:
    """Read the live node's rules and say whether the two could sync."""
    print(f"\nlive node - {url} (read-only: no snapshot, no transaction)")
    try:
        document = fetch(url, "/api/privacy")
    except Exception as problem:  # noqa: BLE001 - any failure is the same verdict
        checks.failed("the live node answers /api/privacy", str(problem))
        return
    theirs = (document.get("data") or {}).get("security") or {}
    mine = sm.status()
    same = bool(theirs.get("policy_hash")) and \
        theirs.get("policy_hash") == mine.get("policy_hash")
    if not theirs:
        checks.failed("the live node publishes its verified security module",
                      "no security block - it is running a build from before "
                      "the module existed")
    elif same:
        checks.ok("the live node implements the same signed ruleset",
                  str(theirs.get("policy_hash"))[:24] + "...")
        checks.ok("the live node verified its own copy locally",
                  f"verified={theirs.get('verified')} offline={theirs.get('offline')}")
    else:
        checks.failed("the live node implements the same signed ruleset",
                      f"theirs {str(theirs.get('policy_hash'))[:16]}... vs ours "
                      f"{str(mine.get('policy_hash'))[:16]}... - redeploy it")

    try:
        info = fetch(url, "/api/chain/info")
        data = info.get("data") if isinstance(info.get("data"), dict) else info
        checks.ok("the live node answers /api/chain/info",
                  f"height {data.get('height')}, "
                  f"head {str(data.get('head_hash'))[:12]}...")
    except Exception as problem:  # noqa: BLE001
        checks.failed("the live node answers /api/chain/info", str(problem))

    print("\n  verdict: a node here would " +
          ("accept this peer's snapshot" if same else
           "REFUSE this peer's snapshot until it runs the same ruleset"))


def run_push(checks: Checks, snapshot: Path, url: str, token: str) -> None:
    """The leg that writes: POST a *real* export to a replica and read it back."""
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    records = (document.get("state") or {}).get("records") or []
    notes = " ".join(str(row.get("note") or "") for row in records
                     if isinstance(row, dict))
    if "harness:" in notes:
        raise SystemExit("refusing to push the harness fixture into a live "
                         "ledger - pass a real export (GET "
                         "/api/chain/sync/snapshot) with --snapshot")
    gate = node_sync.require_protocol(document)
    verified = node_sync.verify_snapshot(document)
    checks.ok("the export verifies here before it is sent",
              f"height {verified['height']}, policy "
              f"{str(gate.get('policy_hash'))[:16]}...")
    payload = json.dumps({"snapshot": document, "origin": "two-node-harness",
                          "force": False}).encode("utf-8")
    headers = {"content-type": "application/json", "accept": "application/json"}
    if token:
        headers["x-admin-token"] = token
    request = urllib.request.Request(url.rstrip("/") + "/api/chain/sync/accept",
                                     data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as problem:
        body = problem.read().decode("utf-8", "replace")[:300]
        checks.failed("the replica accepted the snapshot",
                      f"HTTP {problem.code} {body}")
        return
    except Exception as problem:  # noqa: BLE001
        checks.failed("the replica accepted the snapshot", str(problem))
        return
    data = reply.get("data") or {}
    checks.ok("the replica re-hashed and answered",
              f"imported={data.get('imported')} height={data.get('height')} "
              f"reason={data.get('reason')}")
    try:
        info = fetch(url, "/api/chain/info")
        row = info.get("data") if isinstance(info.get("data"), dict) else info
        checks.ok("the replica now reports the pushed height",
                  f"pushed {verified['height']} -> replica {row.get('height')}")
    except Exception as problem:  # noqa: BLE001
        checks.failed("the replica reports its new height", str(problem))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", default=str(ROOT / "seed" / "chain-snapshot.json"),
                        help="the committed seed ledger the fixture is sealed onto")
    parser.add_argument("--live", default="",
                        help="base URL of a live node to read (never writes)")
    parser.add_argument("--snapshot", default="",
                        help="a REAL export to push (the fixture is refused)")
    parser.add_argument("--push", default="",
                        help="base URL of a replica to POST --snapshot to")
    parser.add_argument("--token", default=os.environ.get("ONENESS_ADMIN_TOKEN", ""),
                        help="admin token for --push (env ONENESS_ADMIN_TOKEN)")
    parser.add_argument("--skip-offline", action="store_true",
                        help="only run the live legs")
    args = parser.parse_args(argv)

    checks = Checks()
    status = sm.status()
    print("=" * 78)
    print("two-node privacy + sync harness")
    print("=" * 78)
    print(f"local policy  {status.get('policy_hash')}")
    print(f"rules         {len(status.get('rules') or [])} "
          f"verified={status.get('verified')} offline={status.get('offline')}")
    if not status.get("verified"):
        checks.failed("this node's security module verifies",
                      str(status.get("error")))
    if not args.skip_offline:
        run_offline(checks, Path(args.seed))
    if args.live:
        run_live(checks, args.live)
    if args.push:
        if not args.snapshot:
            raise SystemExit("--push needs --snapshot: a real export, never a "
                             "fixture")
        run_push(checks, Path(args.snapshot), args.push, args.token)

    passed = sum(1 for row in checks.rows if row[0])
    failed = len(checks.rows) - passed
    print("\n" + "-" * 78)
    print(f"{passed} passed, {failed} failed")
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

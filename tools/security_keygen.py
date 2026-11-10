"""Mint the security issuer key, then sign the privacy policy with it.

    python tools/security_keygen.py            # first time: create everything
    python tools/security_keygen.py --check    # verify what is committed
    python tools/security_keygen.py --rotate   # new key + re-sign (breaks old nodes)

What lands where, and why it is split this way
----------------------------------------------
``security/privacy-policy.json``   committed - the rules every node must apply
``security/privacy-policy.sig``    committed - detached Ed25519 signature (base64)
``security/trusted-keys.json``     committed - the public keys allowed to sign
``data/admin/security-issuer.key`` **never** committed - the signing key itself

The split is the whole point. A node verifies the policy against a key that is
pinned in the repository, so it never has to ask a URL what the rules are. The
private half lives in ``data/admin/`` (git-ignored, next to the chain token), so
signing a new policy is an operator action, not something a running node can do
to itself.

``--rotate`` starts a new trust epoch: nodes still carrying the old
``trusted-keys.json`` refuse every snapshot signed under the new key until they
are updated. That is deliberate - a key rotation is a consensus change.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECURITY_DIR = ROOT / "security"
POLICY_PATH = SECURITY_DIR / "privacy-policy.json"
SIGNATURE_PATH = SECURITY_DIR / "privacy-policy.sig"
TRUSTED_PATH = SECURITY_DIR / "trusted-keys.json"
PRIVATE_KEY_PATH = ROOT / "data" / "admin" / "security-issuer.key"

STANDARD = "oneness-tor/security-module/1"
ISSUER_NAME = "oneness-security-issuer"


def canonical(document: dict) -> bytes:
    """The bytes a signature covers: sorted keys, no ``signature`` member."""
    payload = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")

def policy_document() -> dict:
    """The rules this repository asks every node to enforce.

    Every entry is a rule a node can *check on data it already has*. There is no
    entry that says "trust the other node's word": an operator can always run
    modified code, and no wire format can prove otherwise. What this policy makes
    impossible is for modified code to *sync* - a snapshot carrying a record that
    contradicts its own privacy marker, or a policy hash that is not in the local
    trust set, is refused before a byte of it reaches the ledger.
    """
    return {
        "standard": STANDARD,
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issuer": ISSUER_NAME,
        "privacy": {
            "standard": "oneness-tor/privacy/1",
            "modes": ["public", "anonymous"],
            "default_mode": "public",
            "anonymous_label": "Tor-Anonymous-onion",
            "chain_field": "external_ref",
            "ref_prefix": "oneness-privacy:1",
            #: only a ref beginning with this is a declaration; every other ref
            #: is application payload (order ids, proof markers, "genesis")
            "reserved_prefix": "oneness-privacy:",
            #: a public send writes nothing at all - byte-identical to legacy
            "public_ref": "",
            "anonymous_ref": "oneness-privacy:1|mode=anonymous",
            "masks": ["from"],
            "never_masks": ["to", "amount", "token", "fee", "timestamp",
                            "height", "block", "txid", "signature"],
        },
        "rules": [
            {
                "id": "privacy.mode.known",
                "rule": ("a record that declares a privacy mode must declare "
                         "one of the modes in privacy.modes"),
                "enforced_by": "security_module.verify_record",
                "on_violation": "reject the snapshot",
            },
            {
                "id": "privacy.ref.exact",
                "rule": ("a ref that begins with privacy.reserved_prefix is a "
                         "privacy declaration and must be exactly the anonymous "
                         "ref; a hand-written ref that only resembles one is a "
                         "malformed declaration and the snapshot is refused"),
                "enforced_by": "security_module.verify_record",
                "on_violation": "reject the snapshot",
            },
            {
                "id": "privacy.ref.appdata",
                "rule": ("any other external_ref value is application payload "
                         "and is NOT a privacy declaration - it neither masks "
                         "nor unmaskes anything. The live ledger carries "
                         "'genesis', 'hosted-liveness-proof' and "
                         "'onenness-tor-mainnet:buy:<id>', and a rule that "
                         "forbade them would reject the chain it is protecting"),
                "enforced_by": "security_module.verify_record",
                "on_violation": "nothing to enforce: the ref is ignored here",
            },
            {
                "id": "privacy.marker.agrees",
                "rule": ("the privacy field and external_ref must agree - a "
                         "record that says 'anonymous' in one and 'public' in "
                         "the other is a tampered record, not a typo"),
                "enforced_by": "security_module.verify_record",
                "on_violation": "reject the snapshot",
            },
            {
                "id": "privacy.from.not.masked",
                "rule": ("a chain record may never store the anonymous label as "
                         "its 'from' address: the ledger debits the signer, so a "
                         "stored label is either a lost address or a lie"),
                "enforced_by": "security_module.verify_record",
                "on_violation": "reject the snapshot",
            },
            {
                "id": "privacy.projection.masks",
                "rule": ("every public projection of a record must mask 'from' "
                         "when the record is anonymous"),
                "enforced_by": "privacy.public_view",
                "on_violation": "a page would publish an address the sender hid",
            },
            {
                "id": "peer.policy.signed",
                "rule": ("a peer's snapshot must arrive with the policy it "
                         "implements, and that policy's signature must verify "
                         "against a locally pinned key"),
                "enforced_by": "node_sync.require_protocol",
                "on_violation": "SYNC_PROTOCOL_MISMATCH",
            },
            {
                "id": "peer.policy.same",
                "rule": ("a peer's policy hash must equal a hash this node "
                         "already trusts; a node with different privacy rules "
                         "cannot sync, in either direction"),
                "enforced_by": "node_sync.require_protocol",
                "on_violation": "SYNC_PROTOCOL_MISMATCH",
            },
            {
                "id": "peer.records.verified",
                "rule": ("every record in a snapshot is re-checked against "
                         "privacy.* before import, not just the blocks"),
                "enforced_by": "node_sync.verify_snapshot",
                "on_violation": "reject the snapshot",
            },
        ],
        "peer_sync": {
            "attestation_field": "protocol",
            "requires": "signed policy + matching policy_hash",
            "hash_algorithm": "sha3-256 over canonical JSON of this document",
            "trust_source": "security/trusted-keys.json (committed, never fetched)",
            "offline": ("verification never uses the network: an unreachable "
                        "origin cannot change or block a local decision"),
        },
        "not_claimed": [
            "This is not a zero-knowledge proof system and does not claim to be. "
            "The chain is a transparent-account ledger: balances are public, so "
            "an anonymous transfer masks the sender's address on the record, it "
            "does not make the transfer untraceable (see privacy.py).",
            "No auditor has reviewed this policy. It is a machine-checkable "
            "statement of the rules the code already enforces - calling that "
            "'audited' would be a lie, and the signature proves provenance "
            "(which key signed it), not correctness.",
            "A node can run modified code and still hold a valid key. The gate "
            "stops modified nodes from syncing *data*, which is the only thing "
            "a remote verifier can actually decide.",
        ],
    }


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """Read the issuer key: base64 (one line) or PEM, both Ed25519."""
    raw = path.read_bytes()
    if b"PRIVATE KEY" in raw:
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"{path} is not an Ed25519 key")
        return key
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw.strip()))


def public_b58(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                        format=serialization.PublicFormat.Raw)
    return base58.b58encode(raw).decode("ascii")


def check() -> int:
    """Verify the committed policy against the committed trust list."""
    from oneness_tor.services import security_module

    verified = security_module.verify()
    print(f"standard   {verified['standard']}")
    print(f"policy     {POLICY_PATH}")
    print(f"hash       {verified['hash']}")
    print(f"signer     {verified['signer']['name']} {verified['signer']['pubkey']}")
    print(f"rules      {len(verified['policy']['rules'])} "
          f"({', '.join(rule['id'] for rule in verified['policy']['rules'])})")
    print("OK - the committed policy verifies against the committed trust list")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify the committed policy instead of writing it")
    parser.add_argument("--rotate", action="store_true",
                        help="mint a new issuer key and re-sign (a trust epoch)")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    key_path = PRIVATE_KEY_PATH
    if key_path.exists() and not args.rotate:
        key = load_private_key(key_path)
        print(f"issuer key  {key_path} (existing)")
    else:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        raw = base64.b64encode(key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())).decode("ascii")
        key_path.write_text(raw + "\n", encoding="ascii", newline="\n")
        print(f"issuer key  {key_path} (new - keep it out of git)")

    policy = policy_document()
    signature = base64.b64encode(key.sign(canonical(policy))).decode("ascii")
    write_json(POLICY_PATH, policy)
    SIGNATURE_PATH.write_text(signature + "\n", encoding="ascii", newline="\n")

    pubkey = public_b58(key)
    trusted = {
        "standard": STANDARD,
        "note": ("public keys allowed to sign security/privacy-policy.json. This "
                 "file is committed and reviewed; a node never fetches it. Adding "
                 "a key here is a consensus change."),
        "keys": [{"name": ISSUER_NAME, "pubkey_base58": pubkey,
                  "added": policy["issued_at"]}],
    }
    write_json(TRUSTED_PATH, trusted)
    print(f"policy      {POLICY_PATH}")
    print(f"signature   {SIGNATURE_PATH}")
    print(f"trusted     {TRUSTED_PATH}  pubkey {pubkey}")
    return check()


if __name__ == "__main__":
    raise SystemExit(main())


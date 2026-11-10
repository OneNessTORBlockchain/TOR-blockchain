"""The node's own signed copy of the security rules - verified locally, offline.

Protocol ``oneness-tor/security-module/1``

Why a local signed copy instead of a URL
----------------------------------------
A node that asks a web address "what are the privacy rules?" every time it starts
has two problems: whoever owns that address can change the consensus rules, and a
node that cannot reach it cannot start. Neither is acceptable for a ledger, so the
rules ship *in the repository* as three committed files:

``security/privacy-policy.json``  the rules themselves
``security/privacy-policy.sig``   a detached Ed25519 signature over the canonical
                                 JSON of that document (base64)
``security/trusted-keys.json``    the public keys allowed to sign it

:func:`verify` recomputes the policy hash and checks the signature against a
pinned key. **No network call happens here, ever.** A Render URL is only ever an
optional *refresh source* an operator may copy a new policy from, and the copy
must verify against the same pinned key before it is allowed to replace the local
file - otherwise a compromised origin becomes a consensus change.

What the signature does and does not prove
------------------------------------------
It proves provenance: this document was signed by the key in ``trusted-keys.json``.
It does **not** prove correctness, and no auditor has reviewed it. It also cannot
prove what code a remote node is running - an operator can always run modified
software and still hold a valid key. So the gate does not try to attest to remote
*code*; it checks the only thing a remote verifier can decide, which is the
*data*: a snapshot carrying a record that contradicts its own privacy marker, or
a policy hash this node does not trust, is refused before import.

Nothing here is a zero-knowledge proof, and saying otherwise would be a lie. See
``privacy.py``: the chain is a transparent-account ledger whose balances are
public, so "anonymous" masks the sender's address on the record and nothing more.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.config import ROOT
from core.errors import AppError, ValidationError

#: The document this module speaks.
STANDARD = "oneness-tor/security-module/1"
#: Where a snapshot carries its attestation.
ATTESTATION_FIELD = "protocol"
#: The ref prefix that turns ``external_ref`` into a privacy declaration.
RESERVED_PREFIX = "oneness-privacy:"

_LOCK = threading.RLock()
_CACHE: dict = {"stamp": None, "value": None}


def security_dir() -> Path:
    """Where this node keeps its verified copy (``ONENESS_SECURITY_DIR`` wins).

    The default is the committed ``security/`` directory. An operator who runs a
    node from a read-only image can point this at any local directory that holds
    the three files - the point is that the copy is *local* and verifiable, not
    that it lives next to the code.
    """
    raw = str(os.environ.get("ONENESS_SECURITY_DIR") or "").strip()
    if not raw:
        return ROOT / "security"
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def policy_path() -> Path:
    return security_dir() / "privacy-policy.json"


def signature_path() -> Path:
    return security_dir() / "privacy-policy.sig"


def trusted_path() -> Path:
    return security_dir() / "trusted-keys.json"


def canonical(document: dict) -> bytes:
    """The bytes the signature covers: sorted keys, no ``signature`` member."""
    payload = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def policy_hash(document: dict) -> str:
    """sha3-256 of the canonical document - the identity of a ruleset."""
    return hashlib.sha3_256(canonical(document)).hexdigest()


def _read_json(path: Path, *, what: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as problem:
        raise AppError(
            f"the security module is missing its {what} ({path}) - a node that "
            f"cannot verify the rules does not start",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"path": str(path), "what": what, "error": str(problem)})
    try:
        document = json.loads(raw)
    except ValueError as problem:
        raise AppError(
            f"{path} is not JSON ({problem})",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"path": str(path)})
    if not isinstance(document, dict):
        raise AppError(f"{path} is not a JSON object",
                       code="SECURITY_MODULE_UNVERIFIED", http_status=503,
                       details={"path": str(path)})
    return document


def trusted_keys() -> list[dict]:
    """The pinned public keys, in file order (first match wins when verifying)."""
    document = _read_json(trusted_path(), what="trusted-keys.json")
    if str(document.get("standard") or "") != STANDARD:
        raise AppError(
            f"{trusted_path()} declares {document.get('standard')!r}, not "
            f"{STANDARD!r}",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"path": str(trusted_path())})
    rows = [row for row in (document.get("keys") or [])
            if isinstance(row, dict) and str(row.get("pubkey_base58") or "")]
    if not rows:
        raise AppError(
            f"{trusted_path()} pins no key - nothing could ever be verified",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"path": str(trusted_path())})
    return rows


def _signature_bytes() -> bytes:
    try:
        raw = signature_path().read_text(encoding="ascii").strip()
    except OSError as problem:
        raise AppError(
            f"the security module has no signature ({signature_path()})",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"path": str(signature_path()), "error": str(problem)})
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as problem:  # noqa: BLE001 - any decode failure is the same
        raise AppError(
            f"{signature_path()} is not base64 ({problem})",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"path": str(signature_path())})


def _verify_with(pubkey_base58: str, message: bytes, signature: bytes) -> bool:
    import base58

    try:
        public = base58.b58decode(pubkey_base58)
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def verify(document: dict | None = None, *, signature: bytes | None = None) -> dict:
    """Re-hash and re-verify a policy; return ``{policy, hash, signer, standard}``.

    ``document`` defaults to the local ``security/privacy-policy.json``. The
    policy must also *describe this code* - the modes and refs it lists have to
    match what :mod:`oneness_tor.services.privacy` actually implements. A signed
    document that describes different behaviour is refused: a signature over the
    wrong rules is not a licence to relax them.
    """
    policy = document if document is not None else _read_json(
        policy_path(), what="privacy-policy.json")
    if not isinstance(policy, dict):
        raise AppError("a security policy must be a JSON object",
                       code="SECURITY_MODULE_UNVERIFIED", http_status=503)
    if str(policy.get("standard") or "") != STANDARD:
        raise AppError(
            f"the policy declares {policy.get('standard')!r}, not {STANDARD!r}",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"standard": policy.get("standard")})

    signed = _signature_bytes() if signature is None else signature
    message = canonical(policy)
    signer = None
    for row in trusted_keys():
        if _verify_with(str(row["pubkey_base58"]), message, signed):
            signer = {"name": str(row.get("name") or ""),
                      "pubkey": str(row["pubkey_base58"])}
            break
    if signer is None:
        raise AppError(
            "the security policy's signature does not verify against any pinned "
            "key - this copy of the rules is not one this node trusts",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"policy": str(policy_path()),
                     "trusted": str(trusted_path()),
                     "keys": len(trusted_keys())})

    check_describes_code(policy)
    return {"standard": STANDARD, "policy": policy, "hash": policy_hash(policy),
            "signer": signer, "path": str(policy_path())}


def check_describes_code(policy: dict) -> None:
    """The signed document must describe *this* code, not a more convenient one.

    A signature is only worth something if the thing signed is the rule being
    applied. A policy that lists a mode this node does not implement, or a ref
    string it never writes, is refused even if the signature is perfect - a
    signature over different rules is not a licence to relax these.
    """
    from oneness_tor.services import privacy

    block = policy.get("privacy") if isinstance(policy.get("privacy"), dict) else {}
    described = [str(mode) for mode in (block.get("modes") or [])]
    if sorted(described) != sorted(privacy.MODES):
        raise AppError(
            f"the policy lists modes {described} but this node implements "
            f"{list(privacy.MODES)}",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"policy_modes": described, "code_modes": list(privacy.MODES)})
    for key, mode in (("anonymous_ref", privacy.ANONYMOUS),
                      ("public_ref", privacy.PUBLIC)):
        if str(block.get(key) or "") != privacy.chain_ref(mode):
            raise AppError(
                f"the policy's {key} is {block.get(key)!r} but this node writes "
                f"{privacy.chain_ref(mode)!r}",
                code="SECURITY_MODULE_UNVERIFIED", http_status=503,
                details={"field": key})
    if str(block.get("anonymous_label") or "") != privacy.label():
        raise AppError(
            f"the policy labels an anonymous sender "
            f"{block.get('anonymous_label')!r} but this node publishes "
            f"{privacy.label()!r}",
            code="SECURITY_MODULE_UNVERIFIED", http_status=503,
            details={"field": "anonymous_label"})




def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def require_peer_attestation() -> bool:
    """Must a snapshot carry a signed policy this node trusts?

    Default **yes**: a node whose rules cannot be checked is not a peer, it is an
    unknown. ``ONENESS_SECURITY_REQUIRE_ATTESTATION=0`` exists for one purpose -
    letting an updated node read one last snapshot from a not-yet-updated origin
    during a rollout - and a node running that way says so in :func:`status`.
    """
    return _flag("ONENESS_SECURITY_REQUIRE_ATTESTATION", True)


def allow_unattested() -> bool:
    """Explicit, temporary, and reported: accept a snapshot with no attestation."""
    return _flag("ONENESS_SECURITY_ALLOW_UNATTESTED", False)


def _stamp() -> tuple:
    rows: list[object] = [str(security_dir())]
    for path in (policy_path(), signature_path(), trusted_path()):
        try:
            rows.append(path.stat().st_mtime_ns)
        except OSError:
            rows.append(0)
    return tuple(rows)


def require() -> dict:
    """The verified local policy, cached until one of the three files changes."""
    stamp = _stamp()
    with _LOCK:
        if _CACHE.get("stamp") == stamp and isinstance(_CACHE.get("value"), dict):
            return _CACHE["value"]
    verified = verify()
    with _LOCK:
        _CACHE.update(stamp=stamp, value=verified)
    return verified


def reset() -> None:
    """Forget the cached policy (a rotated file, or a test)."""
    with _LOCK:
        _CACHE.update(stamp=None, value=None)


def signature_text() -> str:
    try:
        return signature_path().read_text(encoding="ascii").strip()
    except OSError as problem:
        raise AppError(f"no policy signature at {signature_path()}",
                       code="SECURITY_MODULE_UNVERIFIED", http_status=503,
                       details={"error": str(problem)})


def trusted_hashes() -> set[str]:
    """Every policy hash this node accepts: the local one, plus any pinned extras.

    ``security/trusted-policies.json`` (``{"hashes": [...]}``) is how a fleet
    moves from one ruleset to the next without a flag day: both hashes are
    trusted while the rollout is in progress, then the old one is deleted.
    """
    hashes = {str(require()["hash"])}
    extra = security_dir() / "trusted-policies.json"
    if extra.is_file():
        try:
            document = json.loads(extra.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = None
        rows = document.get("hashes") if isinstance(document, dict) else None
        for row in rows if isinstance(rows, list) else []:
            text = str(row or "").strip().lower()
            if text:
                hashes.add(text)
    return hashes


def envelope() -> dict:
    """The attestation a snapshot carries: the rules, and the signature on them.

    The whole document travels, not a hash the receiver has to look up: a node
    that has never seen this policy can verify its provenance offline, and a node
    that already trusts a different one refuses it. Copying the envelope is not a
    bypass - a modified node still cannot make a record conform that does not,
    because :func:`verify_record` checks the data itself.
    """
    verified = require()
    return {"standard": STANDARD, "policy_hash": verified["hash"],
            "signer": verified["signer"]["pubkey"],
            "signer_name": verified["signer"]["name"],
            "signature": signature_text(), "policy": verified["policy"]}


def verify_attestation(attestation: object) -> dict:
    """Check a peer's attestation and that its ruleset is one we already trust."""
    if not isinstance(attestation, dict):
        raise ValidationError(
            "this snapshot carries no security attestation - a node that cannot "
            "say which privacy rules it implements cannot sync here",
            code="SYNC_PROTOCOL_MISMATCH",
            details={"expected": ATTESTATION_FIELD, "standard": STANDARD})
    claimed = str(attestation.get("policy_hash") or "").strip().lower()
    document = attestation.get("policy")
    try:
        raw = base64.b64decode(str(attestation.get("signature") or ""),
                               validate=True)
    except Exception:  # noqa: BLE001 - a bad signature encoding is a bad peer
        raise ValidationError(
            "this snapshot's policy signature is not valid base64",
            code="SYNC_PROTOCOL_MISMATCH", details={"policy_hash": claimed})
    # A peer's bad signature is a *peer* problem, not a broken local module: the
    # 503 SECURITY_MODULE_UNVERIFIED that verify() raises for our own files would
    # blame the wrong node, so it is translated into a protocol refusal here.
    try:
        verified = verify(document if isinstance(document, dict) else None,
                          signature=raw)
    except AppError as problem:
        raise ValidationError(
            "this snapshot's policy signature does not verify against a key this "
            "node pins - it is not a ruleset this node trusts",
            code="SYNC_PROTOCOL_MISMATCH",
            details={"policy_hash": claimed, "rule": "peer.policy.signed",
                     "reason": problem.message})
    if verified["hash"] != claimed:
        raise ValidationError(
            "this snapshot's policy does not hash to the hash it claims",
            code="SYNC_PROTOCOL_MISMATCH",
            details={"claimed": claimed, "computed": verified["hash"]})
    known = trusted_hashes()
    if claimed not in known:
        raise ValidationError(
            "this node implements different privacy rules - it cannot sync here",
            code="SYNC_PROTOCOL_MISMATCH",
            details={"their_policy": claimed, "trusted": sorted(known),
                     "fix": ("update security/privacy-policy.json + signature "
                             "from the issuer, or have the peer update")})
    return {"policy_hash": claimed, "signer": verified["signer"],
            "standard": verified["standard"]}


# ------------------------------------------------------- the records themselves
#: Only this prefix turns ``external_ref`` into a privacy declaration. Real
#: application refs on this chain include ``hosted-liveness-proof``, ``genesis``
#: and ``onenness-tor-mainnet:buy:ord_...`` - they are payload, not policy, so
#: they must stay legal. Enforcing "empty or anonymous" would have rejected the
#: live ledger, which is exactly why the audit came before the rule.
def verify_record(record: object, *, policy: dict | None = None) -> dict:
    """Check one chain record's privacy marker against the signed rules.

    Returns ``{"txid", "mode", "ref"}``. Raises :class:`ValidationError` with
    ``code="PRIVACY_UNVERIFIED"`` and the rule id that was broken - this is the
    check that makes the policy more than a document.
    """
    from oneness_tor.services import privacy

    if not isinstance(record, dict):
        raise ValidationError("a chain record must be a JSON object",
                              code="PRIVACY_UNVERIFIED")
    block = policy.get("privacy") if isinstance(policy, dict) else None
    block = block if isinstance(block, dict) else {}
    anonymous_ref = str(block.get("anonymous_ref")
                        or privacy.chain_ref(privacy.ANONYMOUS)).strip()
    public_ref = str(block.get("public_ref")
                     or privacy.chain_ref(privacy.PUBLIC)).strip()
    label = str(block.get("anonymous_label") or privacy.label()).strip()

    txid = str(record.get("txid") or "")
    ref = str(record.get("external_ref") or "").strip()
    declared = record.get("privacy")

    declared_mode: str | None = None
    if declared is not None and str(declared).strip() != "":
        try:
            declared_mode = privacy.normalize(declared)
        except ValidationError:
            raise ValidationError(
                f"record {txid or '(no txid)'} declares privacy "
                f"{str(declared)[:32]!r}, which is not a mode this node implements",
                code="PRIVACY_UNVERIFIED",
                details={"txid": txid, "rule": "privacy.mode.known",
                         "declared": str(declared)[:32],
                         "modes": list(privacy.MODES)})

    if ref == anonymous_ref:
        ref_mode: str | None = privacy.ANONYMOUS
    elif ref == public_ref:
        ref_mode = privacy.PUBLIC
    elif ref.lower().startswith(RESERVED_PREFIX):
        raise ValidationError(
            f"record {txid or '(no txid)'} carries a malformed privacy ref "
            f"{ref[:64]!r} - a declaration must be exactly {anonymous_ref!r}",
            code="PRIVACY_UNVERIFIED",
            details={"txid": txid, "rule": "privacy.ref.exact",
                     "expected": [public_ref, anonymous_ref],
                     "received": ref[:64]})
    else:
        ref_mode = None

    if declared_mode is not None and ref_mode is not None \
            and declared_mode != ref_mode:
        raise ValidationError(
            f"record {txid or '(no txid)'} says {declared_mode!r} in its privacy "
            f"field and {ref_mode!r} in external_ref",
            code="PRIVACY_UNVERIFIED",
            details={"txid": txid, "rule": "privacy.marker.agrees",
                     "privacy": declared_mode, "external_ref": ref_mode})
    if declared_mode == privacy.ANONYMOUS and ref_mode is None:
        raise ValidationError(
            f"record {txid or '(no txid)'} claims to be anonymous but does not "
            f"carry the anonymous ref - the mode would vanish on the next sync",
            code="PRIVACY_UNVERIFIED",
            details={"txid": txid, "rule": "privacy.marker.agrees",
                     "expected_ref": anonymous_ref})

    if str(record.get("from") or "").strip() == label:
        raise ValidationError(
            f"record {txid or '(no txid)'} stores the anonymous label as its "
            f"'from' address - the ledger debits the signer, so that is a lie",
            code="PRIVACY_UNVERIFIED",
            details={"txid": txid, "rule": "privacy.from.not.masked",
                     "label": label})

    return {"txid": txid, "mode": declared_mode or ref_mode or privacy.PUBLIC,
            "ref": ref}




def verify_records(records: object, *, policy: dict | None = None) -> dict:
    """Verify every record in a list; the first violation refuses the whole list."""
    rows = records if isinstance(records, list) else []
    counts = {"records": 0, "public": 0, "anonymous": 0}
    for index, record in enumerate(rows):
        if not isinstance(record, dict):
            continue
        try:
            verified = verify_record(record, policy=policy)
        except ValidationError as problem:
            details = dict(problem.details or {})
            details.setdefault("index", index)
            raise ValidationError(problem.message, code=problem.code,
                                  details=details)
        counts["records"] += 1
        counts[verified["mode"]] = counts.get(verified["mode"], 0) + 1
    return counts


def status() -> dict:
    """What this node verified, and what it refuses to pretend."""
    base = {"standard": STANDARD, "path": str(policy_path()),
            "trusted": str(trusted_path()),
            "require_peer_attestation": require_peer_attestation(),
            "allow_unattested": allow_unattested()}
    try:
        verified = require()
    except AppError as problem:
        return {**base, "verified": False, "code": problem.code,
                "error": problem.message,
                "fix": "python tools/security_keygen.py --check"}
    policy = verified["policy"]
    return {**base, "verified": True, "policy_hash": verified["hash"],
            "signer": verified["signer"],
            "issued_at": policy.get("issued_at"),
            "rules": [str(rule.get("id")) for rule in policy.get("rules") or []
                      if isinstance(rule, dict)],
            "trusted_hashes": sorted(trusted_hashes()),
            "offline": True,
            "not_claimed": policy.get("not_claimed") or []}

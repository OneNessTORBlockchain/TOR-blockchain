"""The signed security module: verified offline, and it refuses the rest.

Every test here is about a decision a node actually makes — start, accept a
snapshot, refuse a peer — never about a string in a document.
"""
from __future__ import annotations

import base64
import inspect
import json
import shutil
from pathlib import Path

import base58
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.config import ROOT
from core.errors import AppError, ValidationError
from oneness_tor.services import node_sync, privacy
from oneness_tor.services import security_module as sm

REAL_SECURITY = ROOT / "security"


@pytest.fixture
def signed_copy(tmp_path, monkeypatch):
    """A private copy of ``security/`` a test may tamper with, then verify."""
    target = tmp_path / "security"
    shutil.copytree(REAL_SECURITY, target)
    monkeypatch.setenv("ONENESS_SECURITY_DIR", str(target))
    sm.reset()
    yield target
    sm.reset()


def resign(folder: Path, policy: dict, key: Ed25519PrivateKey) -> dict:
    """Write a policy that is validly signed — by whichever key the caller names."""
    signature = base64.b64encode(key.sign(sm.canonical(policy))).decode("ascii")
    (folder / "privacy-policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (folder / "privacy-policy.sig").write_text(signature + "\n", encoding="ascii")
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    trusted = {"standard": sm.STANDARD,
               "keys": [{"name": "test-issuer",
                         "pubkey_base58": base58.b58encode(raw).decode("ascii")}]}
    (folder / "trusted-keys.json").write_text(json.dumps(trusted), encoding="utf-8")
    sm.reset()
    return trusted


def test_the_committed_policy_verifies_against_the_committed_trust_list():
    verified = sm.verify()
    assert verified["standard"] == sm.STANDARD
    assert verified["hash"] == sm.policy_hash(verified["policy"])
    assert verified["signer"]["pubkey"] == sm.trusted_keys()[0]["pubkey_base58"]
    assert len(verified["policy"]["rules"]) >= 8


def test_status_is_green_and_says_it_never_fetched_anything():
    document = sm.status()
    assert document["verified"] is True
    assert document["offline"] is True
    assert document["policy_hash"] in document["trusted_hashes"]
    assert "peer.policy.same" in document["rules"]


def test_a_tampered_policy_is_refused_even_though_it_is_still_json(signed_copy):
    path = signed_copy / "privacy-policy.json"
    policy = json.loads(path.read_text(encoding="utf-8"))
    policy["rules"][0]["on_violation"] = "ignore it"
    path.write_text(json.dumps(policy), encoding="utf-8")
    sm.reset()
    with pytest.raises(AppError) as refused:
        sm.verify()
    assert refused.value.code == "SECURITY_MODULE_UNVERIFIED"
    assert "signature" in refused.value.message


def test_a_policy_signed_by_an_unpinned_key_is_refused(signed_copy):
    real_pubkey = sm.trusted_keys()[0]["pubkey_base58"]
    policy = json.loads((signed_copy / "privacy-policy.json").read_text("utf-8"))
    resign(signed_copy, policy, Ed25519PrivateKey.generate())   # signed, but not by us
    # pin the real issuer again: the signature on disk is a stranger's
    (signed_copy / "trusted-keys.json").write_text(json.dumps(
        {"standard": sm.STANDARD,
         "keys": [{"name": "the-real-issuer", "pubkey_base58": real_pubkey}]}),
        encoding="utf-8")
    sm.reset()
    with pytest.raises(AppError) as refused:
        sm.verify()
    assert refused.value.code == "SECURITY_MODULE_UNVERIFIED"
    assert "pinned" in refused.value.message


def test_a_missing_module_stops_a_node_instead_of_defaulting_open(tmp_path, monkeypatch):
    monkeypatch.setenv("ONENESS_SECURITY_DIR", str(tmp_path / "nowhere"))
    sm.reset()
    with pytest.raises(AppError) as refused:
        sm.require()
    assert refused.value.code == "SECURITY_MODULE_UNVERIFIED"
    assert "does not start" in refused.value.message
    assert sm.status()["verified"] is False
    sm.reset()


def test_a_policy_that_describes_different_rules_is_refused():
    policy = sm.require()["policy"]
    wrong_modes = json.loads(json.dumps(policy))
    wrong_modes["privacy"]["modes"] = ["public", "anonymous", "shielded"]
    with pytest.raises(AppError, match="implements"):
        sm.check_describes_code(wrong_modes)

    wrong_ref = json.loads(json.dumps(policy))
    wrong_ref["privacy"]["anonymous_ref"] = "oneness-privacy:2|mode=zk"
    with pytest.raises(AppError, match="anonymous_ref"):
        sm.check_describes_code(wrong_ref)

    wrong_label = json.loads(json.dumps(policy))
    wrong_label["privacy"]["anonymous_label"] = "Somebody"
    with pytest.raises(AppError, match="anonymous sender"):
        sm.check_describes_code(wrong_label)

    sm.check_describes_code(policy)          # the real one passes


def test_the_module_never_opens_a_socket():
    source = inspect.getsource(sm)
    for forbidden in ("import urllib", "import requests", "import socket",
                      "http.client", "urlopen"):
        assert forbidden not in source


# ----------------------------------------------------------- the record rules
def test_application_refs_are_payload_not_privacy_declarations():
    """The shapes the live chain really carries must all stay legal."""
    for ref in ("", "genesis", "hosted-liveness-proof",
                "onenness-tor-mainnet:buy:ord_372668aa2d8d87a9f2",
                "onenness-tor-mainnet:sell:po_5733a5570e8732deff"):
        verified = sm.verify_record({"txid": "t", "external_ref": ref,
                                     "from": "TOR1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaonion"})
        assert verified["mode"] == privacy.PUBLIC


def test_a_real_anonymous_record_is_recognised():
    """Exactly the object block 940 of the live ledger holds."""
    verified = sm.verify_record({
        "txid": "e0460afcc5348f22dd93518cec38f1247af7161399f6c1ec7f92d2d649478ae0",
        "external_ref": "oneness-privacy:1|mode=anonymous",
        "from": "TOR12xd7zywc3iuwcls3fqzsozkgo5ahv3gnonion",
        "to": "TOR1zas7pwn5t7ravd3m4ez63e43ztoivq7conion",
        "amount": 100000000, "fee": 0, "kind": "transfer"})
    assert verified["mode"] == privacy.ANONYMOUS


def test_a_malformed_privacy_ref_is_refused():
    with pytest.raises(ValidationError) as refused:
        sm.verify_record({"txid": "t", "external_ref": "oneness-privacy:1|mode=anon",
                          "from": "TOR1x"})
    assert refused.value.code == "PRIVACY_UNVERIFIED"
    assert refused.value.details["rule"] == "privacy.ref.exact"


def test_a_record_that_contradicts_itself_is_refused():
    with pytest.raises(ValidationError) as refused:
        sm.verify_record({"txid": "t", "privacy": "anonymous",
                          "external_ref": "", "from": "TOR1x"})
    assert refused.value.details["rule"] == "privacy.marker.agrees"


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValidationError) as refused:
        sm.verify_record({"txid": "t", "privacy": "shielded", "from": "TOR1x"})
    assert refused.value.details["rule"] == "privacy.mode.known"


def test_the_masked_label_may_never_be_stored_as_from():
    with pytest.raises(ValidationError) as refused:
        sm.verify_record({"txid": "t", "external_ref": "", "from": privacy.label()})
    assert refused.value.details["rule"] == "privacy.from.not.masked"


def test_verify_records_counts_both_kinds_and_blames_the_right_record():
    counts = sm.verify_records([
        {"txid": "a", "external_ref": "", "from": "TOR1a"},
        {"txid": "b", "external_ref": "oneness-privacy:1|mode=anonymous",
         "from": "TOR1b"}])
    assert counts == {"records": 2, "public": 1, "anonymous": 1}
    with pytest.raises(ValidationError) as refused:
        sm.verify_records([
            {"txid": "a", "external_ref": "", "from": "TOR1a"},
            {"txid": "b", "external_ref": "oneness-privacy:1|mode=anon",
             "from": "TOR1b"}])
    assert refused.value.details["index"] == 1
    assert refused.value.details["txid"] == "b"




# -------------------------------------------------------- the consensus gate
def test_a_peer_on_the_same_ruleset_is_accepted():
    verified = sm.verify_attestation(sm.envelope())
    assert verified["policy_hash"] == sm.require()["hash"]
    assert verified["standard"] == sm.STANDARD


def test_require_protocol_refuses_an_unattested_snapshot(monkeypatch):
    monkeypatch.delenv("ONENESS_SECURITY_ALLOW_UNATTESTED", raising=False)
    monkeypatch.delenv("ONENESS_SECURITY_REQUIRE_ATTESTATION", raising=False)
    with pytest.raises(ValidationError) as refused:
        node_sync.require_protocol({"standard": node_sync.SNAPSHOT_STANDARD})
    assert refused.value.code == "SYNC_PROTOCOL_MISMATCH"
    accepted = node_sync.require_protocol({"protocol": sm.envelope()})
    assert accepted["required"] is True and accepted["attested"] is True


def test_the_rollout_escape_hatch_is_explicit_and_reported(monkeypatch):
    monkeypatch.setenv("ONENESS_SECURITY_ALLOW_UNATTESTED", "1")
    gate = node_sync.require_protocol({})
    assert gate["required"] is False and gate["attested"] is False
    assert "did not verify" in gate["warning"]
    monkeypatch.setenv("ONENESS_SECURITY_REQUIRE_ATTESTATION", "0")
    assert node_sync.require_protocol({})["required"] is False


def test_a_peer_with_different_privacy_rules_cannot_sync(signed_copy, tmp_path,
                                                         monkeypatch):
    """The requirement, stated as a test: different rules => no sync."""
    mine = sm.envelope()
    other_dir = tmp_path / "other-security"
    shutil.copytree(signed_copy, other_dir)
    monkeypatch.setenv("ONENESS_SECURITY_DIR", str(other_dir))
    sm.reset()
    policy = json.loads(json.dumps(sm.require()["policy"]))
    policy["issued_at"] = "2030-01-01T00:00:00Z"          # a different ruleset
    trusted = resign(other_dir, policy, Ed25519PrivateKey.generate())
    theirs = sm.envelope()
    assert theirs["policy_hash"] != mine["policy_hash"]

    # back on MY node: pin their issuer key too, so the *signature* is fine and
    # the only possible reason to refuse is the ruleset itself
    monkeypatch.setenv("ONENESS_SECURITY_DIR", str(signed_copy))
    pinned = json.loads((signed_copy / "trusted-keys.json").read_text("utf-8"))
    pinned["keys"].append({"name": "peer-issuer",
                           "pubkey_base58": trusted["keys"][0]["pubkey_base58"]})
    (signed_copy / "trusted-keys.json").write_text(json.dumps(pinned),
                                                   encoding="utf-8")
    sm.reset()
    with pytest.raises(ValidationError) as refused:
        node_sync.require_protocol({"protocol": theirs})
    assert refused.value.code == "SYNC_PROTOCOL_MISMATCH"
    assert "different privacy rules" in refused.value.message
    assert mine["policy_hash"] in refused.value.details["trusted"]
    # ...while a peer on the same ruleset still passes
    assert node_sync.require_protocol({"protocol": mine})["attested"] is True


def test_a_peer_signed_by_an_unpinned_key_is_a_protocol_mismatch(signed_copy,
                                                                 tmp_path,
                                                                 monkeypatch):
    other_dir = tmp_path / "stranger-security"
    shutil.copytree(signed_copy, other_dir)
    monkeypatch.setenv("ONENESS_SECURITY_DIR", str(other_dir))
    sm.reset()
    policy = json.loads(json.dumps(sm.require()["policy"]))
    policy["issued_at"] = "2031-01-01T00:00:00Z"
    resign(other_dir, policy, Ed25519PrivateKey.generate())
    theirs = sm.envelope()

    monkeypatch.setenv("ONENESS_SECURITY_DIR", str(signed_copy))
    sm.reset()
    with pytest.raises(ValidationError) as refused:
        node_sync.require_protocol({"protocol": theirs})
    assert refused.value.code == "SYNC_PROTOCOL_MISMATCH"
    assert refused.value.details["rule"] == "peer.policy.signed"


def test_a_snapshot_that_hashes_but_lies_about_privacy_is_refused():
    from test_sync_push import _ledger

    node_sync.verify_snapshot(_ledger(2))                  # the honest one passes

    document = _ledger(2)
    record = document["state"]["records"][-1]
    record["external_ref"] = "oneness-privacy:1|mode=anon"  # malformed marker
    block = document["blocks"][-1]["block"]
    block["state_root"] = node_sync.state_root_of(document["state"])
    block["hash"] = node_sync.block_digest(block)
    record["block_hash"] = block["hash"]
    document["state"]["head_hash"] = block["hash"]
    document["head_hash"] = block["hash"]

    with pytest.raises(ValidationError) as refused:
        node_sync.verify_snapshot(document)
    assert refused.value.code == "PRIVACY_UNVERIFIED"
    assert refused.value.details["rule"] == "privacy.ref.exact"

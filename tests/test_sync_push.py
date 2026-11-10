"""The origin pushes, the replica re-hashes: automatic replication that is provable.

A push is only worth automating if a target can never install a ledger it did not
verify, so every assertion here is about the report and about what actually
reached the disk — never about "the thread started".
"""
from __future__ import annotations

import json

import pytest

from core.errors import AppError, ValidationError
from oneness_tor.services import node_sync, security_module

FOUNDER = "TOR1" + "a" * 30 + "onion"


def _ledger(blocks: int = 2) -> dict:
    """A coherent snapshot: linked blocks, matching state root, one tx per block."""
    state = {"chain_id": "072316", "height": 0, "head_hash": "", "genesis_hash": "",
             "total_supply": 50_000_000 * 10 ** 8, "founder": FOUNDER,
             "fee_receiver": FOUNDER,
             "admin_token_sha256": "hash", "created_at": 1, "seq": 1,
             "accounts": {FOUNDER: {"address": FOUNDER, "pubkey_base58": "",
                                    "balance": 50_000_000 * 10 ** 8, "nonce": 0,
                                    "tokens": {}}},
             "tokens": {}, "pools": {}, "records": []}
    ledger: list[dict] = []
    previous = "0" * 64
    for height in range(blocks):
        record = {"txid": f"tx{height}",
                  "kind": "genesis" if height == 0 else "transfer",
                  "from": "genesis" if height == 0 else FOUNDER, "to": FOUNDER,
                  "amount": 1, "token": None, "fee": 0, "note": "",
                  "external_ref": "", "signature": "", "seq": height + 1,
                  "timestamp": height + 1, "height": height, "block_hash": "",
                  "visibility": "public"}
        state["records"].append(record)
        state["height"] = height
        block = {"height": height, "prev_hash": previous, "timestamp": height + 1,
                 "proposer": FOUNDER, "state_root": node_sync.state_root_of(state),
                 "txids": [record["txid"]], "hash": ""}
        block["hash"] = node_sync.block_digest(block)
        record["block_hash"] = block["hash"]
        previous = block["hash"]
        ledger.append({"block": block, "txs": [record]})
    state["head_hash"] = previous
    state["genesis_hash"] = ledger[0]["block"]["hash"]
    return {"standard": node_sync.SNAPSHOT_STANDARD, "chain_id": "072316",
            "height": blocks - 1, "head_hash": state["head_hash"],
            "genesis_hash": state["genesis_hash"], "state": state, "blocks": ledger,
            # a real snapshot always carries the sender's signed policy now
            "protocol": security_module.envelope()}


def _origin(monkeypatch, targets, document=None):
    document = document or _ledger()
    monkeypatch.setattr(node_sync, "origin",
                        lambda: {"url": "", "role": "origin", "configured": False,
                                 "source": "", "origin_token_set": False,
                                 "is_origin_node": False})
    monkeypatch.setattr(node_sync, "is_replica", lambda: False)
    monkeypatch.setattr(node_sync, "push_targets", lambda: targets)
    monkeypatch.setattr(node_sync.node, "sync_export",
                        lambda token=None: {"ok": True, "data": document})
    monkeypatch.setattr(node_sync, "_PUSH_LAST", {"at": None, "ok": None,
                                                 "reason": "no push attempted yet",
                                                 "targets": []})
    return document


def test_push_sends_the_verified_snapshot_and_saves_a_copy(sandbox, monkeypatch):
    targets = [{"url": "https://node2.example", "token": "", "label": "node 2"},
               {"url": "http://node3.onion", "token": "t3", "label": "node 3"}]
    document = _origin(monkeypatch, targets)
    sent: list[dict] = []

    def fake_post(url, headers, payload, timeout):
        sent.append({"url": url, "headers": headers, "payload": payload})
        return {"ok": True, "data": {"imported": True, "skipped": False,
                                     "height": 1, "reason": "snapshot imported"}}

    monkeypatch.setattr(node_sync, "_post_json", fake_post)
    report = node_sync.push()

    assert report["pushed"] == 2 and report["failed"] == []
    assert report["origin_height"] == 1
    assert [row["url"] for row in sent] == [
        "https://node2.example/api/chain/sync/accept",
        "http://node3.onion/api/chain/sync/accept"]
    assert sent[0]["payload"]["snapshot"] is document
    assert sent[1]["headers"]["x-admin-token"] == "t3"
    saved = node_sync.snapshot_dir() / "latest.json"
    assert saved.is_file()
    assert json.loads(saved.read_text(encoding="utf-8"))["head_hash"] == \
        document["head_hash"]
    assert report["saved"].endswith("latest.json")

    status = node_sync.push_status()
    assert status["last_push"]["ok"] is True
    assert status["last_push"]["height"] == 1


def test_a_failing_target_is_reported_never_swallowed(sandbox, monkeypatch):
    targets = [{"url": "https://down.example", "token": "", "label": "down"},
               {"url": "https://node2.example", "token": "", "label": "node 2"}]
    _origin(monkeypatch, targets)

    def fake_post(url, headers, payload, timeout):
        if "down" in url:
            raise AppError("not reachable", code="SYNC_PUSH_UNREACHABLE")
        return {"ok": True, "data": {"imported": False, "skipped": True,
                                     "reason": "already at the origin's height"}}

    monkeypatch.setattr(node_sync, "_post_json", fake_post)
    report = node_sync.push()

    assert report["pushed"] == 1
    assert report["failed"] == ["https://down.example"]
    assert node_sync.push_status()["last_push"]["ok"] is False
    assert {row["url"]: row["ok"] for row in report["targets"]} == {
        "https://down.example": False, "https://node2.example": True}


def test_a_replica_never_pushes(sandbox, monkeypatch):
    monkeypatch.setattr(node_sync, "is_replica", lambda: True)
    monkeypatch.setattr(node_sync, "origin",
                        lambda: {"url": "https://origin.example", "role": "replica",
                                 "configured": True, "source": "config",
                                 "origin_token_set": True, "is_origin_node": False})
    with pytest.raises(AppError) as refused:
        node_sync.push()
    assert refused.value.code == "SYNC_READ_ONLY"


def test_push_without_targets_says_what_to_set(sandbox, monkeypatch):
    _origin(monkeypatch, [])
    with pytest.raises(ValidationError, match="no push targets"):
        node_sync.push()


def test_accept_re_hashes_and_refuses_to_roll_back(sandbox, monkeypatch):
    document = _ledger()
    monkeypatch.setattr(node_sync, "_local_info",
                        lambda: {"height": 9, "head_hash": "b" * 64})
    monkeypatch.setattr(node_sync.node, "sync_import",
                        lambda *a, **k: pytest.fail("must not import backwards"))

    report = node_sync.accept(document, local_token="t")
    assert report["skipped"] is True and report["imported"] is False
    assert "behind" in report["reason"]

    tampered = _ledger()
    tampered["state"]["total_supply"] = 1
    with pytest.raises(ValidationError, match="state_root|does not hash"):
        node_sync.accept(tampered)


def test_accept_imports_a_snapshot_that_is_ahead(sandbox, monkeypatch):
    document = _ledger()
    imported: dict = {}

    def fake_import(payload, token=None):
        imported.update(payload=payload, token=token)
        return {"ok": True, "data": {"height": 1, "head_hash": document["head_hash"],
                                     "blocks": 2, "tokens": 0, "accounts": 1,
                                     "records": 2}}

    monkeypatch.setattr(node_sync, "_local_info",
                        lambda: {"height": 0, "head_hash": "a" * 64})
    monkeypatch.setattr(node_sync.node, "sync_import", fake_import)

    report = node_sync.accept(document, local_token="t", source="node 2")
    assert report["imported"] is True and report["origin"] == "node 2"
    assert imported["payload"] is document and imported["token"] == "t"


def test_old_snapshot_copies_are_pruned(sandbox):
    for blocks in (2, 3, 4, 5):
        node_sync.save_snapshot(_ledger(blocks), keep=2)
    copies = sorted(path.name for path in node_sync.snapshot_dir().glob("chain-*.json"))
    assert copies == ["chain-00000003.json", "chain-00000004.json"]
    assert (node_sync.snapshot_dir() / "latest.json").is_file()


def test_start_pusher_is_a_no_op_without_targets_or_on_a_replica(sandbox, monkeypatch):
    _origin(monkeypatch, [])
    assert node_sync.start_pusher() is None

    monkeypatch.setattr(node_sync, "is_replica", lambda: True)
    monkeypatch.setattr(node_sync, "push_targets",
                        lambda: [{"url": "https://node2.example", "token": "",
                                  "label": "node 2"}])
    assert node_sync.start_pusher() is None


def test_the_push_routes_are_wired(sandbox, monkeypatch):
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "push-test-token")
    from oneness_tor.app import create_app

    http = create_app().test_client()
    auth = {"x-admin-token": "push-test-token"}

    assert http.get("/api/chain/sync/push").status_code == 403
    status = http.get("/api/chain/sync/push", headers=auth).json["data"]
    assert "targets" in status and "last_push" in status

    monkeypatch.setattr(node_sync, "push",
                        lambda **kwargs: {"pushed": 1, "targets": [], "failed": [],
                                          "origin_height": 7, "saved": None})
    pushed = http.post("/api/chain/sync/push", headers=auth, json={})
    assert pushed.status_code == 200 and pushed.json["data"]["pushed"] == 1


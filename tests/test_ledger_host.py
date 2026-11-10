"""The co-located ledger: when the start command spawns a node, and when it refuses.

Everything here is offline: no binary is executed, no seed is downloaded. The
decisions (who owns the process, what a fresh container needs, what a snapshot is
allowed to overwrite) are the part that was silently wrong on Render, where the
API happily served 153 endpoints against a node nobody had started.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from core import ledger_host
from core.errors import ValidationError


def _isolate(monkeypatch, tmp_path, *, url="http://127.0.0.1:8599"):
    monkeypatch.setenv("ONENESS_NODE_API", url)
    monkeypatch.setenv("ONENESS_CHAIN_DIR", str(tmp_path / "chain"))
    monkeypatch.delenv("ONENESS_NODE_SEED", raising=False)
    monkeypatch.delenv("ONENESS_NODE_BINARY", raising=False)
    monkeypatch.delenv("ONENESS_NODE_AUTOSTART", raising=False)


def test_bind_address_only_owns_loopback():
    assert ledger_host.bind_address("http://127.0.0.1:8545") == "127.0.0.1:8545"
    assert ledger_host.bind_address("http://localhost:9001") == "127.0.0.1:9001"
    assert ledger_host.bind_address("http://0.0.0.0:8545") == "127.0.0.1:8545"
    assert ledger_host.bind_address("https://node.example") is None
    assert ledger_host.bind_address("http://10.0.0.5:8545") is None


def test_a_remote_node_is_left_alone(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, url="https://node.example")
    monkeypatch.setattr(ledger_host, "_probe", lambda: False)
    monkeypatch.setattr(ledger_host, "_spawn",
                        lambda *a: pytest.fail("a remote node must never be spawned"))
    result = ledger_host.ensure_local_node(wait=0.1)
    assert result["managed"] is False and result["started"] is False
    assert "another host" in result["hint"]


def test_an_existing_node_is_never_duplicated(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger_host, "_probe", lambda: True)
    monkeypatch.setattr(ledger_host, "_spawn",
                        lambda *a: pytest.fail("a running node must not be restarted"))
    result = ledger_host.ensure_local_node(wait=0.1)
    assert result["reachable"] is True and result["started"] is False
    assert "already serving" in result["hint"]


def test_without_the_binary_nothing_is_spawned(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger_host, "_probe", lambda: False)
    monkeypatch.setattr(ledger_host, "find_binary", lambda: None)
    monkeypatch.setattr(ledger_host, "_spawn",
                        lambda *a: pytest.fail("there is no binary to run"))
    result = ledger_host.ensure_local_node(wait=0.1)
    assert result["started"] is False and "not built" in result["hint"]


def test_without_a_chain_and_without_a_seed_it_refuses(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger_host, "_probe", lambda: False)
    monkeypatch.setattr(ledger_host, "find_binary", lambda: "torchain")
    monkeypatch.setattr(ledger_host, "seed_source", lambda: "")
    monkeypatch.setattr(ledger_host, "_spawn",
                        lambda *a: pytest.fail("a node without a chain would exit"))
    result = ledger_host.ensure_local_node(wait=0.1)
    assert result["started"] is False
    assert "no chain at" in result["hint"] and "ONENESS_NODE_SEED" in result["hint"]


def test_autostart_can_be_switched_off(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ONENESS_NODE_AUTOSTART", "0")
    monkeypatch.setattr(ledger_host, "_probe", lambda: False)
    monkeypatch.setattr(ledger_host, "_spawn",
                        lambda *a: pytest.fail("autostart is disabled"))
    result = ledger_host.ensure_local_node(wait=0.1)
    assert result["started"] is False and "autostart is off" in result["hint"]

    monkeypatch.delenv("ONENESS_NODE_AUTOSTART")
    result = ledger_host.ensure_local_node(autostart=False, wait=0.1)
    assert result["started"] is False and "autostart is off" in result["hint"]


def test_the_command_is_exactly_what_the_node_expects():
    command = ledger_host.command_for("/usr/local/bin/torchain", "127.0.0.1:8545")
    assert command[0].endswith("torchain")
    assert command[1] == "serve" and "--bind" in command and "--dir" in command
    assert command[command.index("--bind") + 1] == "127.0.0.1:8545"


def _binary_layout(monkeypatch, tmp_path):
    """A checkout that carries the committed artifact *and* a compiled ledger."""
    import os

    name = "torchain.exe" if os.name == "nt" else "torchain"
    shipped = tmp_path / "bin" / name
    shipped.parent.mkdir()
    shipped.write_bytes(b"the artifact from an older schema")
    compiled = tmp_path / "usr" / "local" / "bin" / name
    compiled.parent.mkdir(parents=True)
    compiled.write_bytes(b"the ledger this host compiled")

    class Stub:
        chain_binary = tmp_path / "rust" / "torchain" / "target" / "release" / name

        def get(self, key, default=None):
            return default

    monkeypatch.delenv("ONENESS_NODE_BINARY", raising=False)
    monkeypatch.setattr(ledger_host, "settings", Stub())
    monkeypatch.setattr(ledger_host, "ROOT", tmp_path)
    return shipped, compiled


def test_a_compiled_ledger_beats_the_committed_artifact(monkeypatch, tmp_path):
    """The live failure: the image compiled the ledger, the stale artifact won.

    ``bin/torchain`` (built 1 Nov) was chosen over the ``/usr/local/bin`` copy the
    Docker build had just produced, and it answered ``error [BAD_JSON] state.json
    is corrupt: missing field `public_until``` on the new seed — exit 1, every
    chain route 503. A binary built from this source tree always wins.
    """
    shipped, compiled = _binary_layout(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger_host.shutil, "which", lambda _name: str(compiled))
    assert ledger_host.find_binary() == str(compiled)

    # No compiled ledger anywhere: the committed artifact is still a real answer,
    # because a native Render service has no Rust toolchain to build one.
    monkeypatch.setattr(ledger_host.shutil, "which", lambda _name: None)
    assert ledger_host.find_binary() == str(shipped)


def test_the_operator_can_still_pin_the_binary(monkeypatch, tmp_path):
    """``ONENESS_NODE_BINARY`` outranks every guess, compiled or committed."""
    _shipped, compiled = _binary_layout(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger_host.shutil, "which", lambda _name: str(compiled))
    monkeypatch.setenv("ONENESS_NODE_BINARY", str(compiled.parent / "pinned"))
    pinned = compiled.parent / "pinned"
    if pinned.suffix != ".exe" and compiled.suffix == ".exe":
        pinned = pinned.with_suffix(".exe")
    pinned.write_bytes(b"the one the operator named")
    assert ledger_host.find_binary() == str(pinned)


def test_without_any_binary_the_answer_is_none(monkeypatch, tmp_path):
    shipped, _compiled = _binary_layout(monkeypatch, tmp_path)
    shipped.unlink()
    monkeypatch.setattr(ledger_host.shutil, "which", lambda _name: None)
    assert ledger_host.find_binary() is None


def _snapshot(height: int = 2) -> dict:
    return {"state": {"chain_id": "072316", "height": height,
                      "head_hash": "b" * 64, "genesis_hash": "a" * 64,
                      "admin_token_sha256": "origin-node",
                      "accounts": {}, "tokens": {},
                      "records": [{"height": 1, "txid": "t1"},
                                  {"height": 2, "txid": "t2"}]},
            "blocks": [{"height": index} for index in range(height + 1)]}


def test_a_verified_seed_becomes_state_and_blocks(monkeypatch, tmp_path):
    from oneness_tor.services import node_sync

    snapshot = _snapshot()
    monkeypatch.setattr(node_sync, "verify_snapshot", lambda snap: {
        "state": snap["state"], "blocks": snap["blocks"], "height": 2,
        "head_hash": "b" * 64, "genesis_hash": "a" * 64,
        "tokens": 0, "accounts": 0, "records": 2})
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "seed-test-token")

    summary = ledger_host.write_chain(snapshot, target=tmp_path)

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    lines = [json.loads(line) for line in
             (tmp_path / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["height"] == 2 and summary["admin_token_kept"] is True
    assert [row["block"]["height"] for row in lines] == [0, 1, 2]
    assert [tx["txid"] for tx in lines[1]["txs"]] == ["t1"]
    assert [tx["txid"] for tx in lines[2]["txs"]] == ["t2"]
    assert state["admin_token_sha256"] == hashlib.sha3_256(
        b"seed-test-token").hexdigest()
    assert state["chain_id"] == "072316"


def test_an_unverifiable_seed_writes_nothing(monkeypatch, tmp_path):
    from oneness_tor.services import node_sync

    def refuse(_snapshot):
        raise ValidationError("state does not hash to the head block's state_root")

    monkeypatch.setattr(node_sync, "verify_snapshot", refuse)
    target = tmp_path / "chain"
    with pytest.raises(ValidationError):
        ledger_host.write_chain(_snapshot(), target=target)
    assert not target.exists() or not any(target.iterdir())


def test_a_seed_is_read_from_a_path_or_an_envelope(monkeypatch, tmp_path):
    monkeypatch.delenv("ONENESS_NODE_SEED", raising=False)
    seed = tmp_path / "chain-snapshot.json"
    seed.write_text(json.dumps(_snapshot()), encoding="utf-8")
    assert ledger_host.load_seed(str(seed))["state"]["chain_id"] == "072316"

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"ok": True, "data": _snapshot()}),
                       encoding="utf-8")
    assert ledger_host.load_seed(str(wrapped))["state"]["height"] == 2


def test_stop_is_a_no_op_when_this_process_started_nothing():
    assert ledger_host.stop()["stopped"] is False

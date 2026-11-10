"""Wallet generation must find the ledger exactly like the start command does.

The bug these tests pin down: ``ledger.new_wallet`` read ``settings.chain_binary``
(``rust/torchain/target/release/torchain``) directly instead of asking
``core.ledger_host.find_binary``. On every host that never ran ``cargo build`` the
cargo path is empty — a native Render service is cloned into
``/opt/render/project/src`` — so signup answered ``503 CHAIN_UNAVAILABLE`` on
those nodes while the prebuilt Linux ledger shipped at ``bin/torchain`` sat
unused, and only the developer machine (which does have the cargo output) could
create a wallet.

Everything here is offline: the resolver is what is under test, so
``subprocess.run`` is faked and no binary is executed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import ledger_host
from core.errors import ChainUnavailable
from oneness_tor.services import ledger


def _keystore() -> dict:
    return {"version": 1, "kind": "oneness-tor-test", "algorithm": "ed25519",
            "address": "TOR1walletfromtheshippedbinary",
            "pubkey_base58": "pubkeyb58", "secret_key_base58": "secretb58",
            "mnemonic": "one two three four five six seven eight nine ten eleven twelve"}


def _shipped(tmp_path) -> Path:
    """A stand-in for the committed ``bin/torchain`` (an ELF with no cargo build)."""
    path = tmp_path / "bin" / "torchain"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00")
    return path


def test_binary_path_answers_with_whatever_the_resolver_finds(monkeypatch, tmp_path):
    """One resolver, four homes: env → chain.binary → bin/torchain → PATH."""
    shipped = _shipped(tmp_path)
    monkeypatch.setattr(ledger_host, "find_binary", lambda: str(shipped))
    resolved = ledger.binary_path()
    assert resolved == str(shipped)
    # the hardcoded cargo output is not what a host without Rust can run
    assert resolved != str(ledger.settings.chain_binary)


def test_a_wallet_is_generated_with_the_resolved_binary(monkeypatch, sandbox):
    """The Render case: no ``rust/torchain/target``, but ``bin/torchain`` is there."""
    import subprocess

    shipped = _shipped(Path(sandbox))
    monkeypatch.setattr(ledger_host, "find_binary", lambda: str(shipped))
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_keystore()), encoding="utf-8")

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)

    created = ledger.new_wallet("u_render", 24)

    assert seen["argv"][0] == str(shipped), "the shipped ledger must be executed"
    assert seen["argv"][1:3] == ["wallet", "new"]
    assert seen["argv"][seen["argv"].index("--words") + 1] == "24"
    assert created["address"] == _keystore()["address"]
    assert created["mnemonic"] and created["path"].endswith("u_render.json")


def test_no_binary_anywhere_says_where_it_looked(monkeypatch):
    """A missing ledger is one honest 503 that names every place it could live."""
    monkeypatch.setattr(ledger_host, "find_binary", lambda: None)
    with pytest.raises(ChainUnavailable) as failed:
        ledger.binary_path()
    error = failed.value
    assert error.code == "CHAIN_UNAVAILABLE" and error.http_status == 503
    assert "bin/torchain" in error.message
    assert error.details["looked_in"][0] == str(ledger.settings.chain_binary)


def test_an_unrunnable_checkout_is_made_runnable(tmp_path):
    """A zip/artifact deploy loses the exec bit; POSIX still has to start it."""
    shipped = _shipped(tmp_path)
    if os.name == "nt":
        assert ledger._runnable(str(shipped)) == str(shipped)
        return
    shipped.chmod(0o644)
    resolved = ledger._runnable(str(shipped))
    assert os.access(resolved, os.X_OK)
    assert os.stat(resolved).st_mode & 0o111


def test_a_mnemonic_import_falls_back_when_no_binary_exists(monkeypatch):
    """``from-mnemonic`` is a checksum extra: without a binary the Python path runs."""
    monkeypatch.setattr(ledger_host, "find_binary", lambda: None)
    assert ledger._mnemonic_via_binary("one two three") is None

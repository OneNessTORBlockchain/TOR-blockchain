"""The onion identity: a key can be minted *before* a deploy, and it names its own address.

Tor mints the same blob on ``ADD_ONION NEW:ED25519-V3``; generating it here is what
turns "publish the onion after the first boot, then copy the key before the disk is
wiped" into "commit the key and every deploy re-creates that exact .onion".
"""
from __future__ import annotations

import base64

import pytest

from core.errors import TorUnavailable, ValidationError
from oneness_tor import cli
from oneness_tor.services import tor_service

# A fixed seed, so the derivation (clamp + ed25519 public key + rend-spec-v3
# checksum/base32) is locked: if any step changes, this golden pair changes.
SEED = bytes(range(32))
KEY = ("OJTupJxYCu+BaTV2K+BJVZ1tFEDe3hLmoSXxhB//jm8DoQe/884Qvh1w3RjnS8"
       "CZZ+TWMJulDV8d3IZkElUxuA==")
ONION = "daiahiihx7z44ef6dvyn2ghhjpajsz7e2yyjxjinl4o5zbtecjktdoad.onion"


def test_a_key_minted_offline_names_its_own_onion(sandbox):
    made = tor_service.keygen(seed=SEED)

    assert made["key"] == KEY and made["onion"] == ONION
    assert made["url"] == f"http://{ONION}"
    # the blob is a 64-byte ED25519-V3 key: the clamped scalar then the pubkey
    raw = base64.b64decode(made["key"])
    assert len(raw) == 64
    assert raw[0] & 7 == 0 and raw[31] & 128 == 0 and raw[31] & 64 == 64


def test_the_address_survives_the_prefixed_form_tor_reports(sandbox):
    assert tor_service.onion_address(KEY) == ONION
    assert tor_service.onion_address(f"ED25519-V3:{KEY}") == ONION


def test_a_value_that_is_not_a_key_has_no_address(sandbox):
    for broken in ("", "abc123", "<paste the blob here>", "ED25519-V3:"):
        assert tor_service.onion_address(broken) == ""


def test_two_runs_mint_two_identities(sandbox):
    assert tor_service.keygen()["onion"] != tor_service.keygen()["onion"]


def test_a_keygen_requires_a_32_byte_seed(sandbox):
    with pytest.raises(ValidationError) as caught:
        tor_service.keygen(seed=b"short")
    assert "32 bytes" in caught.value.message


def test_keygen_prints_the_env_line_and_the_address(sandbox, capsys):
    assert cli.main(["keygen"]) == 0
    printed = capsys.readouterr().out
    assert "ONENESS_TOR_ONION_KEY=" in printed
    blob = next(line.split("=", 1)[1] for line in printed.splitlines()
                if line.startswith("ONENESS_TOR_ONION_KEY="))
    assert f"http://{tor_service.onion_address(blob)}" in printed


def test_the_committed_key_is_the_address_this_node_owns(sandbox, monkeypatch):
    """``torctl key`` / the boot log can name the onion with no daemon at all."""
    monkeypatch.setattr(tor_service, "SERVICES", type("S", (), {
        "all": staticmethod(lambda: [])})())
    monkeypatch.setattr(tor_service.settings, "get",
                        lambda path, default=None: (KEY if path == "tor.onion_key"
                                                    else default))

    assert tor_service.onion_key_env() == KEY
    assert tor_service.node_key_url() == f"http://{ONION}"
    assert tor_service.node_status()["key_address"] == ONION


def test_install_says_nothing_was_run_when_tor_is_there(sandbox, monkeypatch):
    monkeypatch.setattr(tor_service, "binary", lambda: "/usr/bin/tor")

    installed = tor_service.install_daemon()
    assert installed["already"] is True and installed["installed"] is True
    assert "already installed" in installed["note"]


def test_install_runs_the_host_package_manager_and_reports_it(sandbox, monkeypatch):
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "Setting up tor (0.4.8.10-1) ..."
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return Done()

    monkeypatch.setattr(tor_service.os, "name", "posix")
    monkeypatch.setattr(tor_service.shutil, "which",
                        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    monkeypatch.setattr(tor_service.subprocess, "run", fake_run)
    monkeypatch.setattr(tor_service, "binary",
                        lambda: "/usr/bin/tor" if calls else None)

    installed = tor_service.install_daemon()
    assert installed["installed"] is True and installed["already"] is False
    assert installed["manager"] == "apt-get"
    assert calls == [["apt-get", "update"],
                     ["apt-get", "install", "-y", "--no-install-recommends", "tor"]]
    assert [row["exit"] for row in installed["commands"]] == [0, 0]


def test_install_refuses_to_pretend_on_a_host_it_cannot_change(sandbox, monkeypatch):
    monkeypatch.setattr(tor_service.os, "name", "posix")
    monkeypatch.setattr(tor_service.shutil, "which", lambda name: None)
    monkeypatch.setattr(tor_service, "binary", lambda: None)

    with pytest.raises(TorUnavailable) as caught:
        tor_service.install_daemon()
    assert "package manager" in caught.value.message
    assert caught.value.code == "TOR_DAEMON_UNAVAILABLE"


def test_install_names_the_expert_bundle_on_windows(sandbox, monkeypatch):
    monkeypatch.setattr(tor_service.os, "name", "nt")
    monkeypatch.setattr(tor_service, "binary", lambda: None)

    with pytest.raises(TorUnavailable) as caught:
        tor_service.install_daemon()
    assert "expert bundle" in caught.value.message

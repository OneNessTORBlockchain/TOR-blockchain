"""``torctl`` (``python -m oneness_tor.cli``): the CLI did the thing or said why not.

These tests drive ``main()`` exactly as a shell would — arguments in, exit code and
stdout/stderr out — with the network-facing pieces monkeypatched, so nothing here
needs Tor, a node or a wallet.
"""
from __future__ import annotations

import json

import pytest

from core import pricing
from oneness_tor import cli
from oneness_tor.services import node_sync, tor_service


@pytest.fixture(autouse=True)
def fresh_price_cache(sandbox, monkeypatch):
    monkeypatch.delitem(pricing.settings.raw, "pricing", raising=False)
    monkeypatch.setattr(pricing, "_CACHE",
                        {"mtime": None, "data": {"overrides": {}, "history": []}})


def _fake_tor(monkeypatch, *, reachable: bool = False) -> None:
    monkeypatch.setattr(tor_service, "status", lambda: {
        "reachable": reachable, "control_port": 9051, "control_open": reachable,
        "binary": "/usr/bin/tor" if reachable else None,
        "version": "0.4.8.10" if reachable else None,
        "error": None if reachable else "connection refused",
        "node": {"url": "http://abc.onion" if reachable else None,
                 "key_set": reachable, "port": 8090, "published": reachable}})


def _fake_sync(monkeypatch, *, role: str = "origin", url: str = "",
               targets: list | None = None) -> None:
    monkeypatch.setattr(node_sync, "status", lambda: {
        "role": role, "url": url, "configured": bool(url),
        "last_pull": {"reason": "never"},
        "push": {"enabled": bool(targets), "interval_seconds": 60,
                 "save_dir": "data/snapshots",
                 "targets": targets or [],
                 "last_push": {"reason": "no push attempted yet"}}})


def test_status_reports_what_it_can_actually_see(sandbox, monkeypatch, capsys):
    _fake_tor(monkeypatch)
    _fake_sync(monkeypatch)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "tor            : DOWN" in out
    assert "onion          : not published" in out
    assert "snapshot push  : off" in out


def test_status_with_json_is_machine_readable(sandbox, monkeypatch, capsys):
    _fake_tor(monkeypatch, reachable=True)
    _fake_sync(monkeypatch, targets=[{"url": "https://node2.example",
                                      "label": "node 2", "token_set": False}])

    assert cli.main(["--json", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["onion"] == "http://abc.onion"
    assert payload["push"]["enabled"] is True
    assert payload["push"]["targets"][0]["url"] == "https://node2.example"


def test_fees_list_set_and_reset_from_the_shell(sandbox, capsys):
    assert cli.main(["fees", "list"]) == 0
    listed = capsys.readouterr().out
    assert "contract_create" in listed and "LOCKED" in listed

    assert cli.main(["fees", "set", "meme_banner_7d", "45"]) == 0
    saved = capsys.readouterr().out
    assert "45" in saved and "was" in saved
    assert pricing.price("meme_banner_7d") == 45.0

    assert cli.main(["fees", "reset", "meme_banner_7d"]) == 0
    assert "default" in capsys.readouterr().out
    assert pricing.price("meme_banner_7d") == 30.0


def test_fees_set_on_a_locked_row_needs_the_flag(sandbox, capsys):
    assert cli.main(["fees", "set", "contract_create", "7"]) == 1
    error = capsys.readouterr().err
    assert "PRICE_LOCKED" in error
    assert pricing.price("contract_create") == 5.0

    assert cli.main(["fees", "set", "contract_create", "7", "--confirm-locked"]) == 0
    assert pricing.price("contract_create") == 7.0


def test_fees_set_without_a_price_is_a_usage_error(sandbox):
    with pytest.raises(SystemExit):
        cli.main(["fees", "set", "mint"])


def test_start_names_the_missing_daemon_instead_of_pretending(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("ONENESS_TOR_CONTROL_PORT", "9")      # closed, fast
    monkeypatch.setattr(tor_service, "binary", lambda: None)

    assert cli.main(["start"]) == 1
    error = capsys.readouterr().err
    assert "TOR_DAEMON_UNAVAILABLE" in error
    assert "not installed" in error


def test_start_reports_a_daemon_it_did_not_have_to_start(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("ONENESS_TOR_CONTROL_PORT", "9")
    monkeypatch.setattr(tor_service, "control_reachable", lambda timeout=1.5: True)

    assert cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "already answers" in out


def test_publish_prints_the_onion_and_the_key_to_persist(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(tor_service, "publish_node",
                        lambda port=None, refresh=False: {
                            "onion": "abc.onion", "service_id": "abc",
                            "url": "http://abc.onion", "port": 8090,
                            "reused": False})
    monkeypatch.setattr(tor_service, "onion_key_env", lambda: "BLOBKEY")

    assert cli.main(["publish", "--port", "8090"]) == 0
    out = capsys.readouterr().out
    assert "http://abc.onion" in out
    assert "ONENESS_TOR_ONION_KEY=BLOBKEY" in out


def test_sync_push_reports_every_target(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(node_sync, "push", lambda **kwargs: {
        "origin_height": 12, "pushed": 1, "saved": "data/snapshots/latest.json",
        "targets": [{"url": "https://node2.example", "ok": True,
                     "reason": "snapshot imported"},
                    {"url": "https://node3.example", "ok": False,
                     "reason": "not reachable"}]})

    assert cli.main(["sync", "--push"]) == 0
    out = capsys.readouterr().out
    assert "height         : 12" in out
    assert "pushed         : 1/2" in out
    assert "node3.example: not reachable" in out


def test_snapshot_verifies_before_it_writes(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(node_sync, "snapshot", lambda: {"height": 3})
    monkeypatch.setattr(node_sync, "verify_snapshot",
                        lambda document: {"height": 3, "head_hash": "f" * 64,
                                          "tokens": 2, "accounts": 4})
    written: dict = {}
    monkeypatch.setattr(node_sync, "save_snapshot",
                        lambda document: written.setdefault("path", sandbox / "x.json"))

    assert cli.main(["snapshot"]) == 0
    out = capsys.readouterr().out
    assert "height         : 3" in out
    assert written["path"].name == "x.json"


def test_health_fails_loudly_when_nothing_answers(sandbox, capsys):
    assert cli.main(["health", "--base", "http://127.0.0.1:9"]) == 1
    assert "not reachable" in capsys.readouterr().err


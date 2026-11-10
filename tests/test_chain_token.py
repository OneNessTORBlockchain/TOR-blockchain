"""The chain admin token this deployment keeps for itself.

``torchain init`` prints a token once and the node stores only ``sha3(token)``, so
nothing can look it up. ``data/admin/chain_token`` is the answer: the start
command creates it, writes its sha3 into the ledger before the node boots, and
``require_chain_admin`` hands it to a signed-in admin — nobody pastes anything.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from oneness_tor.services import accounts, auth, chain_token


def _digest(token: str) -> str:
    return hashlib.sha3_256(token.encode("utf-8")).hexdigest()


def _request(header: str, value: str):
    class Fake:
        headers = {header: value}
        args = {}
        is_json = False

        @staticmethod
        def get_json(silent=True):
            return None

    return Fake()


@pytest.fixture
def ledger(sandbox, monkeypatch):
    """A ledger whose admin hash belongs to somebody else's token."""
    monkeypatch.delenv("ONENESS_ADMIN_TOKEN", raising=False)
    chain_dir = sandbox / "chain"
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / "state.json").write_text(json.dumps(
        {"height": 936, "admin_token_sha256": _digest("an-old-token"),
         "records": []}), encoding="utf-8")
    monkeypatch.setenv("ONENESS_CHAIN_DIR", str(chain_dir))
    monkeypatch.setattr(chain_token, "_node_running", lambda: False)
    return chain_dir


def test_ensure_creates_one_token_and_hands_it_to_the_ledger(ledger):
    held = chain_token.ensure()
    assert held["created"] is True and held["adopted"] is True
    token = held["token"]
    assert chain_token.store_path().read_text(encoding="utf-8").strip() == token
    state = json.loads((ledger / "state.json").read_text(encoding="utf-8"))
    assert state["admin_token_sha256"] == _digest(token)
    assert chain_token.usable() == token

    again = chain_token.ensure()
    assert again["created"] is False and again["adopted"] is False
    assert again["token"] == token            # one token per deployment


def test_an_explicit_token_always_wins(ledger, monkeypatch):
    monkeypatch.setenv("ONENESS_ADMIN_TOKEN", "operator-own-token")
    held = chain_token.ensure()
    assert held["token"] == "operator-own-token" and held["source"] == "env"
    assert chain_token.usable() == "operator-own-token"
    assert not chain_token.store_path().exists()      # nothing generated


def test_a_signed_in_admin_gets_chain_level_actions_without_a_token(ledger):
    token = chain_token.ensure()["token"]
    signed = accounts.register_admin("operator", "correct horse battery")
    assert signed["chain_token_bound"] is False
    held = auth.require_chain_admin(
        _request(accounts.ADMIN_HEADER, signed["session"]))
    assert held == token


def test_a_replica_presents_the_origins_token_and_mints_nothing(ledger, monkeypatch):
    """A mirror's ledger was seeded from the origin: it answers to that token."""
    origin_token = "origin-chain-token"
    (ledger / "state.json").write_text(json.dumps(
        {"height": 936, "admin_token_sha256": _digest(origin_token),
         "records": []}), encoding="utf-8")
    monkeypatch.setenv("ONENESS_SYNC_ORIGIN", "https://origin.example")
    monkeypatch.setenv("ONENESS_SYNC_TOKEN", origin_token)
    monkeypatch.setenv("ONENESS_NODE_ROLE", "replica")

    held = chain_token.ensure()
    assert held["created"] is False and held["source"] == "origin"
    assert held["token"] == origin_token
    assert not chain_token.store_path().exists()      # no second secret
    assert chain_token.usable() == origin_token
    assert chain_token.status()["replica"] is True


def test_a_ledger_on_another_token_is_reported_not_overwritten(ledger, monkeypatch):
    chain_token.store_path().write_text("this-deployment-token\n", encoding="utf-8")
    monkeypatch.setattr(chain_token, "_node_running", lambda: True)

    held = chain_token.ensure()
    assert held["adopted"] is False           # never rewritten under a live node
    assert chain_token.usable() is None
    state = json.loads((ledger / "state.json").read_text(encoding="utf-8"))
    assert state["admin_token_sha256"] == _digest("an-old-token")

    accounts.register_admin("operator", "correct horse battery")
    signed = accounts.login_admin("operator", "correct horse battery")
    with pytest.raises(Exception) as problem:
        auth.require_chain_admin(_request(accounts.ADMIN_HEADER, signed["session"]))
    assert getattr(problem.value, "code", "") == "FORBIDDEN"
    message = getattr(problem.value, "message", str(problem.value))
    assert "ONENESS_ADMIN_TOKEN" in message


def test_status_says_where_the_token_comes_from_never_the_token(ledger):
    token = chain_token.ensure()["token"]
    data = chain_token.status()
    assert data["source"] == "file" and data["usable"] is True
    assert "token" not in data
    assert token not in json.dumps(data)


def test_the_admin_surface_reports_the_token_source(ledger):
    chain_token.ensure()
    signed = accounts.register_admin("operator", "correct horse battery")
    from oneness_tor.app import create_app

    http = create_app().test_client()
    reply = http.get("/api/admin/token",
                     headers={accounts.ADMIN_HEADER: signed["session"]})
    assert reply.status_code == 200, reply.get_data(as_text=True)
    data = reply.get_json()["data"]
    assert data["source"] == "file" and data["usable"] is True
    assert http.get("/api/admin/token").status_code == 403

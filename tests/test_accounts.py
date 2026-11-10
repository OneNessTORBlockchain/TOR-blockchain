"""Accounts: username/password wallet signup and the admin security layer."""
from __future__ import annotations

import json

import pytest

from oneness_tor.services import accounts, wallet_access
from oneness_tor.services.auth import require_admin


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def _request(header: str, value: str):
    class Fake:
        headers = {header: value}
        args = {}
        is_json = False

        @staticmethod
        def get_json(silent=True):
            return None

    return Fake()


# --------------------------------------------------------------------- admin
def test_admin_registration_is_first_run_only(sandbox):
    status = accounts.admin_status()
    assert status["registered"] is False and status["registration_open"] is True
    assert status["support_email"] == "support@onenesstech.ca"

    created = accounts.register_admin("operator", "correct horse battery")
    assert created["account"]["role"] == "admin"
    assert created["account"]["username"] == "operator"
    assert created["session"] and created["header"] == "x-admin-session"
    assert created["chain_token_bound"] is False

    with pytest.raises(Exception) as clash:
        accounts.register_admin("someone-else", "another password")
    assert getattr(clash.value, "code", "") == "FORBIDDEN"
    assert accounts.admin_status()["registration_open"] is False


def test_admin_login_and_session_authorize_admin_routes(sandbox):
    accounts.register_admin("operator", "correct horse battery")
    signed = accounts.login_admin("OPERATOR", "correct horse battery")
    assert signed["session"] != ""
    with pytest.raises(Exception):
        accounts.login_admin("operator", "wrong password")

    http = client()
    headers = {accounts.ADMIN_HEADER: signed["session"]}
    assert http.get("/api/admin/account", headers=headers).status_code == 200
    rotated = http.post("/api/admin/account/api-key", headers=headers, json={})
    assert rotated.status_code == 200, rotated.get_data(as_text=True)
    assert rotated.get_json()["data"]["api_key"].startswith("otk_")
    assert http.post("/api/admin/account/api-key", json={}).status_code == 403


def test_the_account_status_route_tells_a_live_session_from_a_dead_one(sandbox):
    """`/admin` heals itself through this route.

    Admin sessions live in the API process, so a redeploy, a restart or the
    2-hour TTL ends them while the browser still holds the token in
    localStorage. ``GET /api/admin/account`` reveals ``username`` only to a
    credential the node still accepts, which is exactly the probe the page uses
    to drop the dead token (it used to say "signed in" while the fee table
    answered FORBIDDEN for ever).
    """
    accounts.register_admin("operator", "correct horse battery")
    live = accounts.login_admin("operator", "correct horse battery")["session"]
    http = client()

    dead = http.get("/api/admin/account",
                    headers={accounts.ADMIN_HEADER: "a-session-this-node-forgot"})
    assert dead.status_code == 200
    payload = dead.get_json()["data"]
    assert payload["registered"] is True          # the account still exists…
    assert payload["username"] is None            # …but nothing here to trust

    alive = http.get("/api/admin/account", headers={accounts.ADMIN_HEADER: live})
    assert alive.get_json()["data"]["username"] == "operator"


def test_rotated_api_key_authorizes_admin_routes_and_old_key_dies(sandbox):
    accounts.register_admin("operator", "correct horse battery")
    first = accounts.rotate_api_key()["api_key"]
    second = accounts.rotate_api_key()["api_key"]
    http = client()
    ok = http.post("/api/admin/account/api-key", headers={"x-admin-token": second}, json={})
    assert ok.status_code == 200, ok.get_data(as_text=True)
    dead = http.post("/api/admin/account/api-key", headers={"x-admin-token": first}, json={})
    assert dead.status_code == 403


def test_password_change_closes_sessions_and_keeps_the_chain_token(sandbox):
    accounts.register_admin("operator", "correct horse battery",
                            chain_token="chain-secret-token")
    signed = accounts.login_admin("operator", "correct horse battery")
    assert signed["chain_token_bound"] is True
    session = accounts.session_from_request(
        _request(accounts.ADMIN_HEADER, signed["session"]))
    assert session["chain_token"] == "chain-secret-token"
    # the raw token must never be in the stored record
    row = accounts.admin_account()
    assert "chain-secret-token" not in json.dumps(row)

    changed = accounts.change_password("correct horse battery", "a much better password")
    assert changed["sessions_closed"] is True
    assert accounts.session_from_request(
        _request(accounts.ADMIN_HEADER, signed["session"])) is None
    relogged = accounts.login_admin("operator", "a much better password")
    session = accounts.session_from_request(
        _request(accounts.ADMIN_HEADER, relogged["session"]))
    assert session["chain_token"] == "chain-secret-token"


def test_username_change_and_duplicate_rejection(sandbox):
    accounts.register_admin("operator", "correct horse battery")
    updated = accounts.change_username("correct horse battery", "operator-two")
    assert updated["account"]["username"] == "operator-two"
    assert accounts.login_admin("operator-two", "correct horse battery")["session"]
    with pytest.raises(Exception):
        accounts.change_username("correct horse battery", "x")


def test_delete_admin_reopens_registration(sandbox):
    accounts.register_admin("operator", "correct horse battery")
    signed = accounts.login_admin("operator", "correct horse battery")
    with pytest.raises(Exception):
        accounts.delete_admin("wrong password")
    removed = accounts.delete_admin("correct horse battery")
    assert removed["deleted"] is True and removed["registration_open"] is True
    assert accounts.admin_status()["registered"] is False
    assert accounts.session_from_request(
        _request(accounts.ADMIN_HEADER, signed["session"])) is None
    again = accounts.register_admin("new-operator", "brand new password")
    assert again["account"]["username"] == "new-operator"


def test_new_admin_wallet_becomes_fee_collector_and_old_file_is_deleted(
        sandbox, make_wallet, monkeypatch):
    accounts.register_admin("operator", "correct horse battery",
                            chain_token="chain-secret-token")
    old = make_wallet("admin_old")
    row = accounts.admin_account()
    accounts.ACCOUNTS.update(row["id"], wallet="admin_old", address=old["address"])
    calls = {}

    def fake_receiver(address, token=None):
        calls["address"], calls["token"] = address, token
        return {"ok": True, "fee_receiver": address}

    monkeypatch.setattr(accounts.node, "set_fee_receiver", fake_receiver)
    result = accounts.generate_admin_wallet("chain-secret-token")
    assert result["address"] == calls["address"] == result["fee_receiver"]
    assert calls["token"] == "chain-secret-token"
    assert result["previous_wallet"] == "admin_old"
    assert result["previous_wallet_deleted"] is True
    assert not (sandbox / "wallets" / "admin_old.json").exists()
    assert (sandbox / "wallets" / f"{result['wallet']}.json").exists()
    assert result["one_time"]["private_key_base58"]
    assert accounts.admin_account()["wallet"] == result["wallet"]


def test_new_admin_wallet_needs_the_chain_token(sandbox):
    accounts.register_admin("operator", "correct horse battery")
    with pytest.raises(Exception) as problem:
        accounts.generate_admin_wallet(None)
    assert getattr(problem.value, "code", "") == "FORBIDDEN"


def test_require_admin_accepts_the_session(sandbox):
    accounts.register_admin("operator", "correct horse battery")
    signed = accounts.login_admin("operator", "correct horse battery")
    assert require_admin(_request(accounts.ADMIN_HEADER, signed["session"])) == "session"


# --------------------------------------------------------------------- users
def test_signup_returns_the_private_key_exactly_once(sandbox, make_wallet):
    created = accounts.signup_user("kathy", "a good password")
    assert created["account"]["role"] == "user"
    assert created["wallet"]["address"].startswith("TOR1")
    assert created["one_time"]["shown_once"] is True
    assert created["one_time"]["private_key_base58"]
    assert created["session"]["session"]

    signed_in = accounts.login_user("kathy", "a good password")
    assert "one_time" not in signed_in
    row = accounts.find_username("kathy")
    assert created["one_time"]["private_key_base58"] not in json.dumps(row)
    assert "mnemonic" not in json.dumps(signed_in)

    with pytest.raises(Exception):
        accounts.login_user("kathy", "not the password")
    with pytest.raises(Exception):
        accounts.signup_user("kathy", "another good password")


def test_wallet_session_reads_only_its_own_wallet(sandbox, make_wallet):
    created = accounts.signup_user("kathy", "a good password")
    other = make_wallet("someone-else")
    headers = {wallet_access.HEADER: created["session"]["session"]}
    http = client()

    mine = http.get("/api/auth/me", headers=headers)
    assert mine.status_code == 200, mine.get_data(as_text=True)
    body = mine.get_json()["data"]
    assert body["account"]["username"] == "kathy"
    assert body["wallet"]["name"] == created["wallet"]["name"]

    assert http.get("/api/auth/me").status_code == 403
    assert http.get(f"/api/wallets/{created['wallet']['name']}/balance",
                    headers=headers).status_code == 200
    # balances are public ledger data: anybody may read any wallet's balance
    public = http.get("/api/wallets/someone-else/balance")
    assert public.status_code == 200
    assert public.get_json()["data"]["address"] == other["address"]
    # …but the receive panel and the private history still need that wallet
    assert http.get("/api/wallets/someone-else/receive").status_code == 403
    assert http.post("/api/auth/logout", headers=headers).status_code == 200
    assert http.get("/api/auth/me", headers=headers).status_code == 403


def test_support_email_is_served(sandbox):
    response = client().get("/api/auth/support")
    assert response.status_code == 200
    assert response.get_json()["data"]["email"] == "support@onenesstech.ca"

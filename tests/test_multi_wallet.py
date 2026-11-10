"""Accounts can hold five wallets — generated or imported — and export keys.

These tests drive the real service layer and the real Flask app. Only the Rust
node's HTTP answers and ``torchain wallet new`` are stubbed (the shared fixtures),
so the crypto that matters — address derivation, the password check and the
sibling-wallet authorization rule — is the production code.
"""
from __future__ import annotations

import base58
import pytest

from oneness_tor.services import accounts, ledger, wallet_access


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def node_account(monkeypatch):
    """Every address answers with a deterministic live balance."""
    monkeypatch.setattr(ledger.node, "account", lambda address: {
        "ok": True, "data": {"address": address, "balance": 2.5,
                             "balance_micro": 250_000_000, "nonce": 1,
                             "tokens": {"MEME": 7}, "role": "holder"}})


def _signup(http, username="kathy"):
    response = http.post("/api/auth/signup",
                         json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]
    return data, {wallet_access.HEADER: data["session"]["session"]}


def test_signup_creates_one_wallet_and_lists_it(sandbox):
    http = client()
    data, headers = _signup(http)
    assert data["account"]["wallet_count"] == 1
    assert data["account"]["max_wallets"] == accounts.MAX_WALLETS == 5

    listing = http.get("/api/auth/wallets", headers=headers)
    assert listing.status_code == 200, listing.get_data(as_text=True)
    body = listing.get_json()["data"]
    assert body["count"] == 1 and body["max_wallets"] == 5
    assert body["wallets"][0]["primary"] is True
    assert body["wallets"][0]["balance_micro"] == 250_000_000


def test_generate_wallets_up_to_the_limit(sandbox):
    http = client()
    data, headers = _signup(http)
    primary = data["wallet"]["name"]

    for index in range(2, 6):
        response = http.post("/api/auth/wallets",
                             json={"password": PASSWORD, "label": f"slot {index}"},
                             headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        generated = response.get_json()["data"]
        assert generated["one_time"]["private_key_base58"]
        assert len(generated["wallets"]) == index
        assert generated["wallet"]["meta"]["label"] == f"slot {index}"

    listing = http.get("/api/auth/wallets", headers=headers).get_json()["data"]
    assert listing["count"] == 5
    assert listing["primary"] == primary

    overflow = http.post("/api/auth/wallets", json={"password": PASSWORD},
                         headers=headers)
    assert overflow.status_code == 400
    assert "limit" in overflow.get_json()["error"]["message"]
    assert len(ledger.wallet_names()) == 5


def test_generate_requires_the_account_password(sandbox):
    http = client()
    _, headers = _signup(http)
    wrong = http.post("/api/auth/wallets", json={"password": "nope-nope"},
                      headers=headers)
    assert wrong.status_code == 403
    assert wrong.get_json()["error"]["code"] == "FORBIDDEN"


def test_import_derives_the_same_address_and_rejects_duplicates(sandbox, make_wallet):
    http = client()
    _, headers = _signup(http)
    external = make_wallet("external")
    secret = external["secret_key_base58"]

    imported = http.post("/api/auth/wallets/import",
                         json={"password": PASSWORD, "secret_key_base58": secret,
                               "label": "phantom"}, headers=headers)
    assert imported.status_code == 200, imported.get_data(as_text=True)
    body = imported.get_json()["data"]
    assert body["imported"] is True
    assert body["wallet"]["address"] == external["address"]
    assert body["wallet"]["meta"]["origin"] == "imported"
    assert body["wallet"]["meta"]["label"] == "phantom"

    again = http.post("/api/auth/wallets/import",
                      json={"password": PASSWORD, "secret_key_base58": secret},
                      headers=headers)
    assert again.status_code == 400
    assert "already in this account" in again.get_json()["error"]["message"]
    assert external["address"] in [ledger.load_wallet(name)["address"]
                                   for name in ledger.wallet_names()]


def test_import_rejects_a_corrupt_phantom_key(sandbox, make_wallet):
    http = client()
    _, headers = _signup(http)
    external = make_wallet("external")
    raw = bytearray(base58.b58decode(external["secret_key_base58"]))
    raw[-1] ^= 0xFF
    broken = base58.b58encode(bytes(raw)).decode("ascii")
    response = http.post("/api/auth/wallets/import",
                         json={"password": PASSWORD, "secret_key_base58": broken},
                         headers=headers)
    assert response.status_code == 400
    assert "tail does not match" in response.get_json()["error"]["message"]


def test_export_is_password_authorized_and_reveals_the_same_key(sandbox, make_wallet):
    http = client()
    data, headers = _signup(http)
    name = data["wallet"]["name"]
    original = ledger.load_wallet(name)

    no_password = http.post(f"/api/auth/wallets/{name}/export", json={},
                            headers=headers)
    assert no_password.status_code == 400

    wrong = http.post(f"/api/auth/wallets/{name}/export",
                      json={"password": "wrong-password"}, headers=headers)
    assert wrong.status_code == 403
    assert "wrong account password" in wrong.get_json()["error"]["message"]

    ok = http.post(f"/api/auth/wallets/{name}/export",
                   json={"password": PASSWORD}, headers=headers)
    assert ok.status_code == 200, ok.get_data(as_text=True)
    exported = ok.get_json()["data"]
    assert exported["secret_key_base58"] == original["secret_key_base58"]
    assert exported["address"] == original["address"]
    assert exported["mnemonic"]
    assert "never share" in exported["warning"]
    assert "no-store" in ok.headers.get("Cache-Control", "")

    # a wallet that is not this account's is refused even with the password
    make_wallet("someone-else")
    other = http.post("/api/auth/wallets/someone-else/export",
                      json={"password": PASSWORD}, headers=headers)
    assert other.status_code == 403


def test_import_rejects_a_bad_recovery_phrase(sandbox):
    """A BIP39 checksum typo is caught by the Rust node instead of silently deriving."""
    from core import ledger_host

    if not ledger_host.find_binary():
        pytest.skip("needs the torchain binary to validate BIP39")
    http = client()
    _, headers = _signup(http)
    response = http.post(
        "/api/auth/wallets/import",
        json={"password": PASSWORD,
              "mnemonic": "one two three four five six seven eight nine ten eleven twelve"},
        headers=headers)
    assert response.status_code == 400
    assert "recovery phrase" in response.get_json()["error"]["message"]


def test_a_session_can_read_from_every_owned_wallet(sandbox, make_wallet):
    http = client()
    data, headers = _signup(http)
    primary = data["wallet"]["name"]
    second = http.post("/api/auth/wallets", json={"password": PASSWORD},
                       headers=headers).get_json()["data"]["wallet"]["name"]

    # the session is bound to the primary, yet the sibling's receive panel works
    receive = http.get(f"/api/wallets/{second}/receive", headers=headers)
    assert receive.status_code == 200, receive.get_data(as_text=True)
    assert receive.get_json()["data"]["name"] == second

    # an unrelated wallet stays private even for a signed-in account
    make_wallet("stranger")
    assert http.get("/api/wallets/stranger/receive",
                    headers=headers).status_code == 403

    # selecting the sibling rotates the session explicitly
    selected = http.post(f"/api/auth/wallets/{second}/select", json={},
                         headers=headers)
    assert selected.status_code == 200, selected.get_data(as_text=True)
    rotated = selected.get_json()["data"]["session"]["session"]
    assert rotated != headers[wallet_access.HEADER]
    me = http.get("/api/auth/me", headers={wallet_access.HEADER: rotated})
    assert me.get_json()["data"]["wallet"]["name"] == second
    assert me.get_json()["data"]["wallet"]["primary"] is False
    assert primary in [row["name"] for row in me.get_json()["data"]["wallets"]]


def test_primary_wallet_changes_the_default_login(sandbox):
    http = client()
    data, headers = _signup(http)
    first = data["wallet"]["name"]
    second = http.post("/api/auth/wallets", json={"password": PASSWORD},
                       headers=headers).get_json()["data"]["wallet"]["name"]

    made = http.post(f"/api/auth/wallets/{second}/primary", json={},
                     headers=headers)
    assert made.status_code == 200, made.get_data(as_text=True)
    assert made.get_json()["data"]["primary"] == second

    login = http.post("/api/auth/login",
                      json={"username": "kathy", "password": PASSWORD})
    assert login.status_code == 200
    assert login.get_json()["data"]["wallet"]["name"] == second
    session = {wallet_access.HEADER: login.get_json()["data"]["session"]["session"]}
    names = [row["name"] for row in
             http.get("/api/auth/wallets", headers=session).get_json()["data"]["wallets"]]
    assert first in names and second in names


def test_repeated_wrong_passwords_are_throttled(sandbox):
    """Ten wrong exports in five minutes, then 429 — no unlimited guessing."""
    http = client()
    data, headers = _signup(http)
    name = data["wallet"]["name"]
    for _ in range(accounts.FAIL_LIMIT):
        response = http.post(f"/api/auth/wallets/{name}/export",
                             json={"password": "still-wrong"}, headers=headers)
        assert response.status_code == 403
    blocked = http.post(f"/api/auth/wallets/{name}/export",
                        json={"password": PASSWORD}, headers=headers)
    assert blocked.status_code == 429
    assert blocked.get_json()["error"]["code"] == "TOO_MANY_ATTEMPTS"
    assert blocked.get_json()["error"]["details"]["limit"] == accounts.FAIL_LIMIT


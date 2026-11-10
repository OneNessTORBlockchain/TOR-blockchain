"""The public/operator split: only /admin and /ollama are the operator's.

The node operator hosts the ledger and offers it to the public; visitors sign up
with a username + password, get a real Ed25519 wallet, and everything they do is
paid by and owned by *that* wallet. No page asks for the chain admin token — the
two operator pages use the admin username/password session instead.

These tests drive the real Flask app (only the Rust node is stubbed where a
wallet session has to be minted).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.config import ROOT
from oneness_tor.services import wallet_access

PUBLIC_PAGES = ["/", "/dashboard", "/explorer", "/wallet", "/payments", "/contracts",
                "/workbench", "/meme", "/nft", "/docs", "/coin"]
TEMPLATES = ROOT / "oneness_tor/web/templates"


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def _headers(name: str, address: str) -> dict:
    session = wallet_access.open_session(name, address)
    return {wallet_access.HEADER: session["session"]}


# --------------------------------------------------------- the token is gone
def test_no_public_page_asks_for_the_admin_token(sandbox):
    http = client()
    for path in PUBLIC_PAGES:
        response = http.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        body = response.get_data(as_text=True)
        assert "Admin token" not in body, f"{path} renders an admin-token button"
        assert "token-dialog" not in body, f"{path} renders the admin-token dialog"
        assert "x-admin-token" not in body, f"{path} mentions x-admin-token"


def test_only_the_two_operator_pages_are_admin(sandbox):
    admin = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    ollama = (TEMPLATES / "ollama.html").read_text(encoding="utf-8")
    # the operator pages gate on the admin session, never on a pasted token
    assert "x-admin-session" in admin and "x-admin-session" in ollama
    assert "admin-gate" in ollama
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name in ("admin.html", "ollama.html"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "x-admin-token" not in text, f"{path.name} still mentions x-admin-token"
        assert "Admin token" not in text, f"{path.name} still renders an admin-token button"


def test_the_admin_page_drops_a_session_the_node_no_longer_accepts():
    """A deploy ends every admin session; the browser keeps the dead token.

    Without the probe the account card said "signed in" while the fee table (and
    every other operator button) answered FORBIDDEN with no way back in.
    """
    admin = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    assert "!status.username" in admin            # the probe on GET /api/admin/account
    assert "session ended" in admin               # …and it says why + what to do
    assert "needsSignIn" in admin                 # a FORBIDDEN points at sign-in


def test_the_shared_runtime_no_longer_holds_a_token():
    script = (ROOT / "oneness_tor/web/static/js/app.js").read_text(encoding="utf-8")
    assert "x-admin-token" not in script
    assert "oneness.adminToken" not in script
    assert "askToken" not in script
    # the account wallet is the public identity
    assert '/api/auth/me' in script



# ----------------------------------------------------- the visitor's own wallet
def test_wallet_page_is_the_signed_in_wallet(sandbox, make_wallet, monkeypatch):
    from oneness_tor.services import ledger

    wallet = make_wallet("u_kathy")
    monkeypatch.setattr(ledger.node, "account", lambda address: {"ok": True, "data": {
        "address": address, "balance": 3.5, "balance_micro": 350_000_000,
        "nonce": 2, "tokens": {"MEME": 12}, "role": "holder"}})
    monkeypatch.setattr(ledger.node, "public_records", lambda limit=500: {
        "ok": True, "data": {"records": []}})

    http = client()
    headers = _headers("u_kathy", wallet["address"])

    me = http.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.get_data(as_text=True)
    data = me.get_json()["data"]
    assert data["wallet"]["name"] == "u_kathy"
    assert data["wallet"]["address"] == wallet["address"]
    assert data["wallet"]["balance_micro"] == 350_000_000

    # the wallet page is public HTML and needs no admin credential at all
    page = http.get("/wallet").get_data(as_text=True)
    assert '/api/auth/me' in page
    assert '/api/wallets"' not in page          # no admin wallet list
    assert "Your wallet" in page and "Your AI-NFTs" in page

    # every own-wallet read the page performs works on the session alone
    for path in ("/api/wallets/u_kathy",
                 "/api/wallets/u_kathy/balance",
                 "/api/wallets/u_kathy/receive",
                 "/api/wallets/u_kathy/history"):
        response = http.get(path, headers=headers)
        assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_anyone_can_read_a_balance_but_only_the_owner_can_spend(sandbox, make_wallet):
    make_wallet("u_kathy")
    other = make_wallet("u_sam")
    http = client()

    # a balance is permanent public ledger data — even a stranger may read it
    response = http.get("/api/wallets/u_kathy/balance")
    assert response.status_code == 200
    assert response.get_json()["data"]["name"] == "u_kathy"

    # …but the receive panel (and the private history / spending) stay owner-only
    denied = http.get("/api/wallets/u_kathy/receive",
                      headers=_headers("u_sam", other["address"]))
    assert denied.status_code == 403
    assert "sign in at /wallet" in denied.get_json()["error"]["message"]


# ------------------------------------------------------------------ ownership
def test_workbench_project_belongs_to_the_session_wallet(sandbox, make_wallet):
    kathy = make_wallet("u_kathy")
    sam = make_wallet("u_sam")
    http = client()
    created = http.post("/api/workbench/projects",
                        json={"name": "Kathy Shop", "kind": "web"},
                        headers=_headers("u_kathy", kathy["address"]))
    assert created.status_code == 200, created.get_data(as_text=True)
    project = created.get_json()["data"]["project"]
    assert project["owner_wallet"] == "u_kathy"
    assert project["owner_address"] == kathy["address"]

    # the owner may write its own files without any admin credential
    saved = http.post(f"/api/workbench/projects/{project['id']}/files",
                      json={"path": "notes.md", "content": "hello\n"},
                      headers=_headers("u_kathy", kathy["address"]))
    assert saved.status_code == 200, saved.get_data(as_text=True)

    # a different visitor cannot touch it
    denied = http.post(f"/api/workbench/projects/{project['id']}/files",
                       json={"path": "notes.md", "content": "mine now\n"},
                       headers=_headers("u_sam", sam["address"]))
    assert denied.status_code == 403

    # and nobody can create a project without signing in
    assert http.post("/api/workbench/projects", json={"name": "Anon"}).status_code == 403


def test_invoice_payment_accepts_the_payers_wallet_session(sandbox, make_wallet,
                                                            monkeypatch):
    from oneness_tor.services import payments

    wallet = make_wallet("u_kathy")
    other = make_wallet("u_sam")
    seen: dict = {}

    def fake_pay(invoice_id: str, name: str) -> dict:
        seen.update(invoice=invoice_id, wallet=name)
        return {"invoice": {"id": invoice_id, "status": "paid"}, "txid": "aa" * 32,
                "height": 3}

    monkeypatch.setattr(payments, "pay_invoice", fake_pay)
    http = client()

    paid = http.post("/api/payments/invoices/inv_0001/pay", json={},
                     headers=_headers("u_kathy", wallet["address"]))
    assert paid.status_code == 200, paid.get_data(as_text=True)
    assert seen == {"invoice": "inv_0001", "wallet": "u_kathy"}

    denied = http.post("/api/payments/invoices/inv_0001/pay",
                       json={"wallet": "u_kathy"},
                       headers=_headers("u_sam", other["address"]))
    assert denied.status_code == 403


@pytest.mark.parametrize("path,body", [
    ("/api/nft/mint", {"name": "x", "symbol": "X"}),
    ("/api/meme/tokens", {"name": "x", "symbol": "X"}),
])
def test_paid_public_actions_ask_for_a_wallet_not_a_token(sandbox, path, body):
    http = client()
    response = http.post(path, json=body)
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert "sign in at /wallet" in error["message"]
    assert "x-admin-token" not in response.get_data(as_text=True)


# ----------------------------------------- the numbers when scripts are blocked
def _stub_live(monkeypatch, *, reachable: bool = True):
    """Point the page snapshot at a known chain so the assertions are exact."""
    from types import SimpleNamespace

    from core.errors import ChainUnavailable
    from oneness_tor.services import live_snapshot

    def fake_call(method: str, path: str, **_kwargs: object) -> dict:
        if not reachable:
            raise ChainUnavailable("the TOR node at http://127.0.0.1:8545 is unreachable")
        if path == "/api/chain/info":
            return {"ok": True, "data": {
                "height": 33, "chain_id": "072316",
                "head_hash": "e0" * 32, "genesis_hash": "58" * 32,
                "founder": "TOR1founderaddressxxxxxxxxxxxxxxxxxxxxnonion",
                "fee_receiver": "TOR1feereceiverxxxxxxxxxxxxxxxxxxnonion",
                "total_supply_micro": 5_000_000_000_000_000}}
        if path.startswith("/api/account/"):
            return {"ok": True, "data": {"balance_micro": 1_250_000_000}}
        raise AssertionError(f"unexpected page probe {path}")

    monkeypatch.setattr(live_snapshot, "node", SimpleNamespace(
        base_url="http://127.0.0.1:8545", call=fake_call))
    monkeypatch.setattr(live_snapshot, "ollama", SimpleNamespace(status=lambda: {
        "installed": True, "reachable": True, "version": "0.40.6",
        "models": [], "model_count": 7, "default_model": "llama3"}))
    monkeypatch.setattr(live_snapshot, "tor_service", SimpleNamespace(status=lambda: {
        "installed": True, "reachable": True, "version": "0.4.9",
        "control_port": 9051, "socks_port": 9050,
        "services": [{"onion": "aaaaaaaaaaaaaaaa.onion"}]}))
    monkeypatch.setattr(live_snapshot, "payments", SimpleNamespace(revenue=lambda: {
        "total_micro": 1_000_000_000, "total_tor": "10", "count": 2}))
    live_snapshot.clear()
    return live_snapshot


def test_the_page_carries_the_numbers_when_scripts_are_blocked(sandbox, monkeypatch):
    """Tor Browser blocks JS on http onion pages — the values must be in the HTML."""
    live_snapshot = _stub_live(monkeypatch)
    try:
        response = client().get("/")
    finally:
        live_snapshot.clear()
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "height <b>33</b>" in body                 # the header pill, no script
    assert 'id="s-height">33<' in body                # the chain-height card
    assert 'id="s-chain">072316' in body
    assert 'id="s-supply">50000000<' in body          # microTOR → TOR, as the API does
    assert 'id="s-founder">12.5<' in body
    assert "7 models" in body                         # the model badge
    assert "1 hidden services" in body                # tor control card
    assert ">10 TOR<" in body and ">2 paid<" in body  # settlement card
    assert 'class="nojs"' in body                     # still explained, not hidden


def test_a_dead_node_still_renders_the_page(sandbox, monkeypatch):
    """The snapshot is a convenience: nothing in it may break a page."""
    live_snapshot = _stub_live(monkeypatch, reachable=False)
    try:
        response = client().get("/")
    finally:
        live_snapshot.clear()
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "node <b>down</b>" in body
    assert "the TOR node at http://127.0.0.1:8545 is unreachable" in body
    assert 'id="s-height">—<' in body
    assert 'id="s-supply">—<' in body


def test_the_snapshot_is_reused_instead_of_probing_on_every_page(sandbox,
                                                                 monkeypatch):
    live_snapshot = _stub_live(monkeypatch)
    calls: list[str] = []
    probe = live_snapshot.node.call

    def counting(method: str, path: str, **kwargs: object) -> dict:
        calls.append(path)
        return probe(method, path, **kwargs)

    monkeypatch.setattr(live_snapshot.node, "call", counting)
    try:
        first = live_snapshot.snapshot()
        second = live_snapshot.snapshot()
        live_snapshot.clear()
        third = live_snapshot.snapshot()
    finally:
        live_snapshot.clear()
    assert second is first                      # cached inside SNAPSHOT_TTL
    assert third is not first                   # …and dropped by clear()
    assert calls == ["/api/chain/info",
                     "/api/account/TOR1founderaddressxxxxxxxxxxxxxxxxxxxxnonion",
                     "/api/chain/info",
                     "/api/account/TOR1founderaddressxxxxxxxxxxxxxxxxxxxxnonion"]


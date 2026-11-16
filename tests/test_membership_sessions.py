"""Two wallets side by side: one VM each, one product each, and the five messages.

This is the seam the hosted webapp needs (``TORanonymouschat.md`` §12): the chain
knows **which VM serves which wallet in which product**, so one visitor cannot be
handed two machines, two visitors cannot share one, and a webapp VM cannot be
re-used as an API VM. Only the Rust node's HTTP answers are stubbed; the leases,
the proofs, the invoice, the settlement checks and the free counter are real code.
"""
from __future__ import annotations

import pytest

from oneness_tor.services import ledger, membership, wallet_sessions
from test_membership_products import (PRICE_MICRO, client,  # noqa: F401
                                      fixed_rate, node_stub, post,
                                      signed_headers)


def open_vm(http, wallet: dict, product: str = "webapp", **body):
    """``POST /membership/<address>/sessions`` with this wallet's signature."""
    return post(http, f"/membership/{wallet['address']}/sessions", wallet,
                {"product": product, **body})


def leases(http, wallet: dict, product: str = ""):
    """``GET /membership/<address>/sessions`` -- signed, so never public."""
    path = f"/membership/{wallet['address']}/sessions"
    if product:
        path += f"?product={product}"
    return http.get(path, headers=signed_headers(wallet, "GET", path, None))


def pay(http, wallet: dict, name: str, *, product: str = "webapp") -> dict:
    """One real period: invoice, a signed Ed25519 transfer, verified settlement."""
    opened = post(http, f"/membership/{wallet['address']}/invoices", wallet,
                  {"months": 1, "product": product}).get_json()["data"]
    invoice = opened["invoice"]
    payment = ledger.send_micro(name, invoice["treasury"], PRICE_MICRO,
                                note=opened["memo"])
    settled = post(http, f"/membership/{wallet['address']}/settle", wallet,
                   {"invoice_id": invoice["id"], "txid": payment["txid"]})
    assert settled.status_code == 200, settled.get_json()
    return settled.get_json()["data"]


# ------------------------------------------------ two wallets, side by side
def test_two_wallets_get_two_separate_vms_in_two_products(make_wallet, node_stub,
                                                         fixed_rate):
    first = make_wallet("founder")          # the webapp visitor
    second = make_wallet("second")          # the API key holder
    http = client()

    webapp_vm = open_vm(http, first, "webapp").get_json()["data"]
    api_vm = open_vm(http, second, "api").get_json()["data"]

    # two VMs, two wallets, two names, and neither is the other's
    assert webapp_vm["reused"] is False and api_vm["reused"] is False
    assert webapp_vm["session"]["wallet"] == first["address"]
    assert api_vm["session"]["wallet"] == second["address"]
    assert webapp_vm["tenant"] != api_vm["tenant"]
    assert webapp_vm["tenant"].startswith("webapp-ws_")
    assert api_vm["tenant"].startswith("api-ws_")
    # both were granted on the free tier: five messages, no subscription
    assert webapp_vm["mode"] == "free" and api_vm["mode"] == "free"
    assert webapp_vm["free_messages_left"] == 5
    assert webapp_vm["session"]["state"] == "live"

    # each wallet sees exactly its own machine, in its own namespace
    mine = leases(http, first).get_json()["data"]
    theirs = leases(http, second).get_json()["data"]
    assert mine["count"] == 1 and mine["live_by_product"]["webapp"] == 1
    assert mine["live_by_product"]["api"] == 0
    assert theirs["count"] == 1 and theirs["live_by_product"]["api"] == 1
    assert theirs["live_by_product"]["webapp"] == 0
    assert {row["tenant"] for row in mine["sessions"]} == {webapp_vm["tenant"]}


def test_one_wallet_gets_one_vm_and_replace_retires_the_old_one(make_wallet,
                                                                node_stub,
                                                                fixed_rate):
    first = make_wallet("founder")
    http = client()

    opened = open_vm(http, first, "webapp").get_json()["data"]
    again = open_vm(http, first, "webapp").get_json()["data"]
    assert again["reused"] is True
    assert again["session"]["id"] == opened["session"]["id"]
    assert again["tenant"] == opened["tenant"]      # the same VM, not a second one

    replaced = open_vm(http, first, "webapp", replace=True).get_json()["data"]
    assert replaced["reused"] is False
    assert replaced["session"]["id"] != opened["session"]["id"]
    assert leases(http, first).get_json()["data"]["live_by_product"]["webapp"] == 1

    dead = post(http, f"/membership/{first['address']}/sessions/"
                      f"{opened['session']['id']}/heartbeat", first, {})
    assert dead.status_code == 409
    error = dead.get_json()["error"]
    assert error["code"] == "SESSION_CLOSED"
    assert error["details"]["state"] == "replaced"

    # asking for a *different* machine while one is live is a 409, never a silent
    # substitution: the product must say replace=True to retire what it holds
    with pytest.raises(Exception) as clash:
        wallet_sessions.open_(first["address"], "webapp", tenant="another-box")
    assert getattr(clash.value, "http_status", None) == 409
    assert getattr(clash.value, "code", "") == "SESSION_EXISTS"
    assert getattr(clash.value, "details", {})["tenant"] == replaced["tenant"]


def test_a_vm_never_serves_two_wallets_and_a_webapp_vm_is_never_an_api_vm(
        make_wallet, node_stub, fixed_rate):
    first = make_wallet("founder")
    second = make_wallet("second")
    http = client()
    webapp_vm = open_vm(http, first, "webapp").get_json()["data"]

    # 1. another wallet cannot be given the same machine
    stolen = open_vm(http, second, "api", tenant=webapp_vm["tenant"])
    assert stolen.status_code == 403
    details = stolen.get_json()["error"]["details"]
    assert details["held_by"] == first["address"]
    assert details["held_product"] == "webapp"
    assert details["asked_product"] == "api"

    # 2. the same wallet cannot re-use its webapp machine for its API key
    crossed = open_vm(http, first, "api", tenant=webapp_vm["tenant"])
    assert crossed.status_code == 403
    assert "never an API VM" in crossed.get_json()["error"]["message"]

    # 3. two products, two namespaces, two machines -- and both are its own
    api_vm = open_vm(http, first, "api", tenant="api-box-1").get_json()["data"]
    assert api_vm["tenant"] == "api-box-1"
    both = leases(http, first).get_json()["data"]
    assert both["live_by_product"] == {"webapp": 1, "api": 1, "desktop": 0}


def test_a_wallet_with_no_membership_and_no_free_message_gets_no_vm(
        make_wallet, node_stub, fixed_rate):
    third = make_wallet("third")
    http = client()
    for turn in range(5):
        spent = post(http, f"/membership/{third['address']}/use-free", third,
                     {"product": "webapp", "note": f"turn {turn}"})
        assert spent.status_code == 200

    walled = open_vm(http, third, "webapp")
    assert walled.status_code == 402
    error = walled.get_json()["error"]
    assert error["code"] == "MEMBERSHIP_REQUIRED"
    assert error["details"]["price_usd"] == 11.99
    assert error["details"]["days"] == 30
    assert error["details"]["subscribe"].endswith("/invoices")
    assert error["details"]["sessions"].endswith("/sessions")
    assert wallet_sessions.listing(third["address"])["count"] == 0

    # the subscription is the other door, and it opens the same call
    pay(http, third, "third")
    paid = open_vm(http, third, "webapp").get_json()["data"]
    assert paid["mode"] == "member" and paid["session"]["live"] is True


def test_sessions_need_the_wallet_signature_and_a_known_product(make_wallet,
                                                                node_stub,
                                                                fixed_rate):
    first = make_wallet("founder")
    second = make_wallet("second")
    http = client()
    path = f"/membership/{first['address']}/sessions"

    assert http.post(path, json={"product": "webapp"}).status_code == 401
    assert http.get(path).status_code == 401

    foreign = post(http, path, second, {"product": "webapp"})
    assert foreign.status_code == 403
    assert "different wallet" in foreign.get_json()["error"]["message"]

    typo = open_vm(http, first, "webbap")
    assert typo.status_code == 400
    assert typo.get_json()["error"]["details"]["scopes"] == list(
        wallet_sessions.SCOPES)

    unsafe = open_vm(http, first, "webapp", tenant="not a container name")
    assert unsafe.status_code == 400
    assert "container name" in unsafe.get_json()["error"]["message"]



def test_heartbeat_renews_the_lease_and_the_clock_ends_it(make_wallet, node_stub,
                                                         fixed_rate):
    first = make_wallet("founder")
    http = client()
    opened = open_vm(http, first, "webapp", ttl=60).get_json()["data"]
    identifier = opened["session"]["id"]
    heartbeat = f"/membership/{first['address']}/sessions/{identifier}/heartbeat"

    renewed = post(http, heartbeat, first, {"ttl": 3600}).get_json()["data"]
    assert renewed["session"]["live"] is True
    assert renewed["extended_seconds"] == 3600 - 60
    assert renewed["session"]["expires_unix"] > opened["session"]["expires_unix"]
    assert renewed["allowed"] is True and renewed["reason"] == "free"

    lapsed = membership.identity(first["address"],
                                 now=renewed["session"]["expires_unix"] + 1)
    assert lapsed["free_messages_left"] == 5          # the lease died, not the tier
    assert wallet_sessions.active(first["address"], "webapp",
                                 now=renewed["session"]["expires_unix"] + 1) is None

    with pytest.raises(Exception) as gone:
        wallet_sessions.heartbeat(first["address"], identifier,
                                  now=renewed["session"]["expires_unix"] + 1)
    assert getattr(gone.value, "http_status", None) == 409
    assert getattr(gone.value, "code", "") == "SESSION_EXPIRED"

    closed = post(http, f"/membership/{first['address']}/sessions/{identifier}"
                        "/close", first, {"reason": "sign-out"}).get_json()["data"]
    assert closed["closed"] is True and closed["already"] is False
    twice = post(http, f"/membership/{first['address']}/sessions/{identifier}"
                       "/close", first, {}).get_json()["data"]
    assert twice["already"] is True
    assert leases(http, first).get_json()["data"]["live"] == 0


def test_cancelling_the_membership_releases_the_vm(make_wallet, node_stub,
                                                  fixed_rate):
    first = make_wallet("founder")
    http = client()
    pay(http, first, "founder")
    opened = open_vm(http, first, "webapp").get_json()["data"]
    assert opened["mode"] == "member"

    ended = post(http, f"/membership/{first['address']}/cancel", first,
                 {"reason": "operator"}).get_json()["data"]
    assert ended["cancelled"] is True
    assert ended["sessions_closed"] == 1
    assert ended["sessions"] == [opened["session"]["id"]]

    dead = post(http, f"/membership/{first['address']}/sessions/"
                      f"{opened['session']['id']}/heartbeat", first, {})
    assert dead.status_code == 409
    assert leases(http, first).get_json()["data"]["live"] == 0


def test_the_free_counter_records_which_vm_spent_the_message(make_wallet,
                                                            node_stub,
                                                            fixed_rate):
    first = make_wallet("founder")
    http = client()
    opened = open_vm(http, first, "webapp").get_json()["data"]
    identifier = opened["session"]["id"]

    spent = post(http, f"/membership/{first['address']}/use-free", first,
                 {"product": "webapp", "session": identifier, "note": "turn 1"})
    assert spent.get_json()["data"]["left"] == 4
    state = membership.free_state(first["address"])
    assert state["last_session"] == identifier
    assert state["turns"][-1]["session"] == identifier
    assert state["turns"][-1]["product"] == "webapp"



def test_verify_tx_answers_without_activating_anything(make_wallet, node_stub,
                                                      fixed_rate):
    first = make_wallet("founder")
    second = make_wallet("second")
    http = client()
    opened = post(http, f"/membership/{first['address']}/invoices", first,
                  {"months": 1, "product": "webapp"}).get_json()["data"]
    invoice = opened["invoice"]

    nothing = http.get(f"/membership/{first['address']}/verify-tx/{'00' * 32}"
                       f"?invoice_id={invoice['id']}").get_json()["data"]
    assert nothing["valid"] is False and nothing["activates"] is False
    assert nothing["checks"]["on_ledger"] is False
    assert "not on this node" in nothing["reason"]

    # a real transfer to the treasury: every rule true, and still nothing active
    payment = ledger.send_micro("founder", invoice["treasury"], PRICE_MICRO,
                                note=opened["memo"])
    good = http.get(f"/membership/{first['address']}/verify-tx/{payment['txid']}"
                    f"?invoice_id={invoice['id']}").get_json()["data"]
    assert good["valid"] is True
    assert all(good["checks"].values()) is True
    assert good["activates"] is False
    assert good["expected"]["tor"] == invoice["amount_tor"]
    assert http.get(f"/membership/{first['address']}").get_json()["data"][
        "active"] is False

    # the same money, aimed somewhere else, is not a payment to the treasury
    elsewhere = "TOR1" + "q" * 30 + "onion"
    wrong = ledger.send_micro("founder", elsewhere, PRICE_MICRO)
    off = http.get(f"/membership/{first['address']}/verify-tx/{wrong['txid']}"
                   f"?invoice_id={invoice['id']}").get_json()["data"]
    assert off["valid"] is False and off["checks"]["receiver"] is False
    assert "treasury" in off["reason"]

    # and a wallet that did not pay cannot claim somebody else's transfer
    other = http.get(f"/membership/{second['address']}/verify-tx/"
                     f"{payment['txid']}").get_json()["data"]
    assert other["valid"] is False and other["checks"]["sender"] is False

    post(http, f"/membership/{first['address']}/settle", first,
         {"invoice_id": invoice["id"], "txid": payment["txid"]})
    fresh = post(http, f"/membership/{first['address']}/invoices", first,
                 {"months": 1, "product": "webapp"}).get_json()["data"]
    reused = http.get(f"/membership/{first['address']}/verify-tx/"
                      f"{payment['txid']}?invoice_id={fresh['invoice']['id']}"
                      ).get_json()["data"]
    assert reused["valid"] is False and reused["checks"]["unused"] is False
    assert "already settled" in reused["reason"]


"""Prove a hosted node's wallet, funding, subscription and NFT doors are live.

The unit tests prove the rules; this script proves a *running* API -- a laptop
stack or a deployed one -- with nothing but HTTP and real Ed25519 signatures.
Four things are exercised, in this order:

1. **Every door is online** -- a read-only survey of the wallet, contract,
   payment, studio and chain routes. A route that answers 200, 403 or 405
   exists (a 405 is a write-only door refusing the verb, a 403 is a guarded
   door refusing the caller); only 404 means this build does not have the door.
2. **A hosted node wallet** -- ``POST /api/auth/signup`` creates a real wallet
   whose key never leaves the node, and the returned ``x-wallet-session`` is
   what spends from it. The wallet's own doors are then read *with* the session
   and refused *without* it (that 403 is a promise, not a nuisance), and a
   wrong signature is refused at the challenge door.
3. **Funding** -- one signed TOR transfer from ``--funder`` (a keystore you
   hold; the founder wallet by default) credits the test wallet, so every later
   fee is paid on the real chain with real money, not by a fixture.
4. **The subscription and the native NFT** -- the funded wallet pays the 5 TOR
   ``contract_create`` price for a ``subscription`` program and calls
   ``subscribe`` / ``renew`` / ``expired``; then it creates a supply-1 edition
   (``token_create``) and moves it with ``nft_transfer`` **signed by the node**
   through ``POST /api/wallets/<name>/sign`` -- the wallet never hands its key
   to this script.

    python tools/live_hosted_wallet_walkthrough.py
    python tools/live_hosted_wallet_walkthrough.py --base http://127.0.0.1:8090 \\
        --fund 18 --symbol PROBE01 --to TOR1...onion --wait 120

Exit codes: ``0`` every promise held, ``1`` one broke (the report names which),
``2`` the NFT doors never appeared inside ``--wait`` (a deploy that has not
landed yet). One run per symbol: the edition must still be held by the wallet
when the move happens, so a second run against the same chain needs a new
``--symbol``.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.torchain_signer import sign_transaction  # noqa: E402

SESSION_HEADER = "x-wallet-session"
DEFAULT_RECIPIENT = "TOR1cv6ldgzsndtlodhbjy3fwbzgp5r5vyvoonion"
MICRO_PER_TOR = 100_000_000

#: Read-only survey: ``(path, what the door is)``. ``GET`` only, so nothing is
#: written; a 405 still proves the route is registered.
DOORS = [
    ("/api/health", "the API itself"),
    ("/api/chain/info", "chain height, supply, fee receiver"),
    ("/api/auth/me", "the signed-in wallet (403 without a session)"),
    ("/api/wallets", "the operator's wallet list (admin only)"),
    ("/api/contracts/templates", "the Rust contract templates"),
    ("/api/contracts", "every deployed program"),
    ("/api/payments/plans", "the LOCKED price table"),
    ("/api/nft/catalogue", "the AI-NFT studio catalogue"),
    ("/api/chain/nft", "every unique edition on chain"),
    ("/api/account/TOR111111111111111111111111111111onion/holdings",
     "one account's TOR + tokens + NFTs"),
]


def call(session: requests.Session, base: str, path: str, body: dict | None = None,
         wallet_session: str | None = None, **params) -> dict:
    """One HTTP call, flattened: ``{status, ok, data, error, code, message}``."""
    headers = {SESSION_HEADER: wallet_session} if wallet_session else {}
    if body is None:
        reply = session.get(base + path, headers=headers, params=params or None,
                            timeout=120)
    else:
        reply = session.post(base + path, json=body, headers=headers, timeout=120)
    try:
        payload = reply.json()
    except ValueError:
        return {"status": reply.status_code, "ok": False, "data": None,
                "error": None, "code": "NOT_JSON", "message": reply.text[:400]}
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    return {"status": reply.status_code, "ok": bool(payload.get("ok")),
            "data": payload.get("data"), "error": error or None,
            "code": str(error.get("code") or ""),
            "message": str(error.get("message") or "")}


def data_of(step: dict | None) -> dict:
    value = (step or {}).get("data")
    return value if isinstance(value, dict) else {}


def wait_for_doors(session: requests.Session, base: str, seconds: float) -> str:
    """``""`` when the NFT doors answer, otherwise the reason they do not."""
    deadline = time.time() + seconds
    while True:
        try:
            reply = session.get(base + "/api/chain/nft", timeout=60)
            if reply.status_code == 200:
                return ""
            problem = f"/api/chain/nft answered {reply.status_code}"
        except requests.RequestException as error:
            problem = f"/api/chain/nft unreachable: {str(error)[:160]}"
        if time.time() >= deadline:
            return f"{problem} for {seconds:.0f}s"
        time.sleep(15)


# --------------------------------------------------------------- 1. the doors
def survey(session: requests.Session, base: str) -> dict:
    """Read every door once; the status *is* the answer."""
    rows: dict = {}
    for path, label in DOORS:
        step = call(session, base, path)
        rows[path] = {"label": label, "status": step["status"],
                      "code": step["code"], "message": step["message"]}
    return rows


# ---------------------------------------------------- 2. the hosted node wallet
def open_wallet(session: requests.Session, base: str, username: str,
                password: str) -> dict:
    """Sign up (or log in) and keep the wallet + the session that spends it."""
    signup = call(session, base, "/api/auth/signup",
                  {"username": username, "password": password})
    how = "signup"
    reply = signup
    if signup["status"] != 200:
        # a taken username is the normal case for a re-run: log in instead
        reply = call(session, base, "/api/auth/login",
                     {"username": username, "password": password})
        how = "login"
    body = data_of(reply)
    wallet = body.get("wallet") if isinstance(body.get("wallet"), dict) else {}
    held = body.get("session") if isinstance(body.get("session"), dict) else {}
    one_time = body.get("one_time") if isinstance(body.get("one_time"), dict) else {}
    return {
        "how": how, "signup_status": signup["status"],
        "signup_error": signup["message"],
        "status": reply["status"], "message": reply["message"],
        "wallet": {"name": wallet.get("name"), "address": wallet.get("address"),
                   "pubkey_base58": wallet.get("pubkey_base58")},
        "session": held.get("session"), "header": held.get("header") or SESSION_HEADER,
        "expires_at": held.get("expires_at"),
        # the key is shown once by the node: prove it exists, never store it
        "one_time_key_returned": bool(one_time.get("private_key_base58")),
        "message_signed": None,
    }


def wallet_doors(session: requests.Session, base: str, wallet: dict,
                 held: str) -> dict:
    """The wallet's own doors: read with the session, refused without it."""
    name = str(wallet.get("name"))
    address = str(wallet.get("address"))
    rows = {
        "me": call(session, base, "/api/auth/me", wallet_session=held),
        "profile": call(session, base, f"/api/wallets/{name}"),
        "balance": call(session, base, f"/api/wallets/{name}/balance"),
        "receive_owner": call(session, base, f"/api/wallets/{name}/receive",
                              wallet_session=held),
        "history_owner": call(session, base, f"/api/wallets/{name}/history",
                              wallet_session=held),
        "receive_without_session": call(session, base,
                                        f"/api/wallets/{name}/receive"),
        "challenge": call(session, base, f"/api/wallets/{name}/challenge", {}),
        "sign_without_session": call(session, base, f"/api/wallets/{name}/sign",
                                     {"kind": "transfer", "to": address,
                                      "amount": 0.00000001}),
        "account": call(session, base, f"/api/account/{address}"),
        "holdings": call(session, base, f"/api/account/{address}/holdings"),
        "my_nfts": call(session, base, f"/api/account/{address}/nfts"),
    }
    # a signature that is not the wallet's own must not open a session
    rows["session_with_bad_signature"] = call(
        session, base, f"/api/wallets/{name}/session",
        {"nonce": (data_of(rows["challenge"]).get("nonce") or "not-a-nonce"),
         "signature": "0" * 88})
    return rows


# ----------------------------------------------------------------- 3. funding
def fund_wallet(session: requests.Session, base: str, wallet: dict, funder: dict,
                tor: float, note: str) -> dict:
    """One real signed transfer: the funder pays, the test wallet receives."""
    micro = int(round(float(tor) * MICRO_PER_TOR))
    if micro <= 0:
        return {"skipped": "nothing to fund (--fund 0)"}
    seen = call(session, base, f"/api/account/{funder['address']}")
    nonce = int(data_of(seen).get("nonce") or 0)
    signed = sign_transaction(funder, {
        "kind": "transfer", "from": funder["address"], "to": wallet["address"],
        "amount": micro, "token": None, "note": note, "external_ref": "",
        "nonce": nonce, "timestamp": int(time.time())})
    sent = call(session, base, "/api/chain/tx", signed)
    balance = call(session, base, f"/api/wallets/{wallet['name']}/balance")
    for _ in range(20):
        if int(data_of(balance).get("balance_micro") or 0) >= micro:
            break
        time.sleep(3)
        balance = call(session, base, f"/api/wallets/{wallet['name']}/balance")
    records = data_of(sent).get("records") or []
    txid = next((row.get("txid") for row in records
                 if isinstance(row, dict) and row.get("kind") == "transfer"), None)
    return {"status": sent["status"], "message": sent["message"],
            "txid": txid, "funder": funder["address"],
            "nonce": nonce, "amount_micro": micro, "amount_tor": float(tor),
            "funder_balance_micro": data_of(seen).get("balance_micro"),
            "balance": balance, "balance_micro": data_of(balance).get("balance_micro")}


# ------------------------------------------------- 4a. the token / edition leg
def create_edition(session: requests.Session, base: str, wallet: dict, held: str,
                   symbol: str, name: str) -> dict:
    """The node's own wallet signs a supply-1 ``token_create`` - a native NFT.

    No metadata URI is sent on purpose: ``/api/wallets/<name>/sign`` carries the
    privacy mode in ``external_ref`` (that is what keeps one masking rule), so
    publishing a document from the hosted door needs a new field, not a guess.
    A keystore run of ``tools/nft_live_walkthrough.py`` can still publish one.
    """
    rows: dict = {}
    rows["balance_before"] = call(session, base,
                                  f"/api/wallets/{wallet['name']}/balance")
    signed = call(session, base, f"/api/wallets/{wallet['name']}/sign",
                  {"kind": "token_create", "to": wallet["address"], "amount": 1,
                   "token": symbol, "note": name, "privacy": "public"},
                  wallet_session=held)
    rows["sign"] = signed
    rows["create"] = call(session, base, "/api/chain/tx",
                          data_of(signed).get("signed_tx") or {})
    rows["account"] = call(session, base, f"/api/account/{wallet['address']}")
    rows["holdings"] = call(session, base,
                            f"/api/account/{wallet['address']}/holdings")
    rows["edition"] = call(session, base, f"/api/chain/nft/{symbol}")
    rows["balance_after"] = call(session, base,
                                 f"/api/wallets/{wallet['name']}/balance")
    return rows


# ---------------------------------------------------- 4b. the subscription leg
def deploy_subscription(session: requests.Session, base: str, wallet: dict,
                        held: str, name: str) -> dict:
    """Pay the 5 TOR ``contract_create`` price, then subscribe/renew/expired."""
    rows: dict = {"templates": call(session, base, "/api/contracts/templates")}
    deploy = call(session, base, "/api/contracts",
                  {"name": name, "template": "subscription", "wallet": wallet["name"]},
                  wallet_session=held)
    rows["deploy"] = deploy
    contract = data_of(deploy).get("contract") or {}
    contract_id = contract.get("id")
    rows["contract_id"] = contract_id
    if contract_id:
        for function in ("subscribe", "renew", "expired"):
            rows[function] = call(session, base,
                                  f"/api/contracts/{contract_id}/call",
                                  {"function": function}, wallet_session=held)
        rows["read"] = call(session, base, f"/api/contracts/{contract_id}",
                            wallet_session=held)
        rows["mine"] = call(session, base, "/api/contracts/mine",
                            wallet_session=held)
    return rows


# --------------------------------------------------------- 4c. the NFT transfer
def move_edition(session: requests.Session, base: str, wallet: dict, held: str,
                 symbol: str, to: str, note: str) -> dict:
    """Sign ``nft_transfer`` on the node and move the edition to ``to``."""
    rows: dict = {
        # the door checks the shape before the node ever sees it
        "wrong_kind": call(session, base, "/api/chain/nft/transfer",
                           {"kind": "transfer", "from": wallet["address"],
                            "to": to, "amount": 1, "token": symbol,
                            "nonce": 0, "timestamp": 0}),
    }
    signed = call(session, base, f"/api/wallets/{wallet['name']}/sign",
                  {"kind": "nft_transfer", "to": to, "amount": 1,
                   "token": symbol, "note": note, "privacy": "public"},
                  wallet_session=held)
    rows["sign"] = signed
    payload = data_of(signed).get("signed_tx") or {}
    rows["transfer"] = call(session, base, "/api/chain/nft/transfer", payload)
    receipt = data_of(rows["transfer"]).get("receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    rows["txid"] = receipt.get("txid")
    rows["edition_after"] = call(session, base, f"/api/chain/nft/{symbol}")
    rows["holdings_recipient"] = call(session, base, f"/api/account/{to}/nfts")
    rows["holdings_sender"] = call(session, base,
                                   f"/api/account/{wallet['address']}/nfts")
    rows["history"] = call(session, base, "/api/chain/nft/history",
                           symbol=symbol, limit=5)
    if rows["txid"]:
        rows["receipt"] = call(session, base, f"/api/chain/nft/tx/{rows['txid']}")
        rows["record"] = call(session, base, f"/api/chain/tx/{rows['txid']}")
    rows["replay"] = call(session, base, "/api/chain/nft/transfer", payload)
    return rows


# ------------------------------------------------------------------ the verdict
def verdict(report: dict) -> list[str]:
    """Every promise this walkthrough makes, checked against the answers."""
    steps = report["steps"]
    wallet = report.get("wallet") or {}
    address = str(wallet.get("address") or "")
    to = str(report.get("recipient") or "")
    symbol = str(report.get("symbol") or "")
    broken: list[str] = []

    def want(label: str, ok: bool, detail: str = "") -> None:
        if not ok:
            broken.append(f"{label}{': ' + detail if detail else ''}")

    # 1. the doors ---------------------------------------------------------
    doors = steps.get("doors") or {}
    missing = sorted(path for path, row in doors.items() if row.get("status") == 404)
    want("every surveyed door exists on this build", not missing, ", ".join(missing))
    subscription = steps.get("subscription") or {}
    offered = data_of(subscription.get("templates")).get("templates") or []
    want("the `subscription` template is offered",
         any(row.get("id") == "subscription" for row in offered
             if isinstance(row, dict)))

    # 2. the hosted node wallet --------------------------------------------
    opened = steps.get("wallet_opened") or {}
    want("signup (or login) opened a wallet", opened.get("status") == 200,
         str(opened.get("message")))
    want("the wallet has a real TOR1...onion address",
         address.startswith("TOR1") and address.endswith("onion"), address or "none")
    if opened.get("how") == "signup":
        want("signup showed the one-time private key",
             opened.get("one_time_key_returned") is True)
    want("the session is what the doors accept",
         bool(opened.get("session")) and opened.get("header") == SESSION_HEADER)

    empty = steps.get("before_funding") or {}
    want("the wallet had no chain account before funding",
         empty.get("status") == 404, f"status {empty.get('status')}")

    own = steps.get("wallet_doors") or {}
    me = data_of(own.get("me"))
    want("the session reads its own wallet",
         (own.get("me") or {}).get("status") == 200 and me.get("address") == address,
         str(me.get("address")))
    want("the balance door answers to anyone",
         (own.get("balance") or {}).get("status") == 200,
         str((own.get("balance") or {}).get("status")))
    want("the receive panel opens for the owner",
         (own.get("receive_owner") or {}).get("status") == 200,
         str((own.get("receive_owner") or {}).get("message")))
    want("the history opens for the owner",
         (own.get("history_owner") or {}).get("status") == 200,
         str((own.get("history_owner") or {}).get("message")))
    want("the receive panel refuses a stranger",
         (own.get("receive_without_session") or {}).get("status") == 403,
         str((own.get("receive_without_session") or {}).get("status")))
    want("signing refuses a stranger",
         (own.get("sign_without_session") or {}).get("status") == 403,
         str((own.get("sign_without_session") or {}).get("status")))
    want("the challenge door hands out a nonce",
         (own.get("challenge") or {}).get("status") == 200
         and bool(data_of(own.get("challenge")).get("nonce")),
         str((own.get("challenge") or {}).get("message")))
    want("a wrong signature cannot open a session",
         (own.get("session_with_bad_signature") or {}).get("status") in (400, 401, 403),
         str((own.get("session_with_bad_signature") or {}).get("status")))
    want("the holdings pane answers with the account",
         (own.get("holdings") or {}).get("status") == 200,
         str((own.get("holdings") or {}).get("message")))
    want("the NFT holdings door answers",
         (own.get("my_nfts") or {}).get("status") == 200,
         str((own.get("my_nfts") or {}).get("message")))

    # 3. funding -----------------------------------------------------------
    funding = steps.get("funding") or {}
    if funding.get("skipped"):
        want("the wallet already held what the fees need", True)
    else:
        want("the funder's transfer is accepted",
             funding.get("status") == 200, str(funding.get("message")))
        want("the funding transfer has a txid", bool(funding.get("txid")))
        want("the funded balance arrived",
             int(funding.get("balance_micro") or 0)
             >= int(funding.get("amount_micro") or 0),
             f"{funding.get('balance_micro')} of {funding.get('amount_micro')}")

    # 4a. the token / edition ---------------------------------------------
    edition = steps.get("edition") or {}
    signing = data_of(edition.get("sign"))
    want("the node signs the token_create for its own wallet",
         (edition.get("sign") or {}).get("status") == 200
         and bool(signing.get("signed_tx")),
         str((edition.get("sign") or {}).get("message")))
    want("the node's signature is a token amount, not microTOR",
         signing.get("unit") == "token" and signing.get("amount_value") == 1,
         f"unit={signing.get('unit')} amount={signing.get('amount_value')}")
    want("token_create is accepted",
         (edition.get("create") or {}).get("status") == 200,
         str((edition.get("create") or {}).get("message")))
    held_tokens = data_of(edition.get("account")).get("tokens")
    held_tokens = held_tokens if isinstance(held_tokens, dict) else {}
    want("the wallet holds the whole supply",
         int(held_tokens.get(symbol) or 0) == 1, str(held_tokens.get(symbol)))
    before = int(data_of(edition.get("balance_before")).get("balance_micro") or 0)
    after = int(data_of(edition.get("balance_after")).get("balance_micro") or 0)
    want("the chain charged its 5 TOR creation fee",
         before - after == 5 * MICRO_PER_TOR, f"{before - after} microTOR")
    minted = data_of(edition.get("edition"))
    want("the edition door names the wallet as the holder",
         minted.get("owner") == address and minted.get("supply") == 1,
         f"owner={minted.get('owner')} supply={minted.get('supply')}")
    want("the creator is the same wallet",
         (minted.get("creator") or minted.get("owner")) == address,
         str(minted.get("creator")))

    # 4b. the subscription -------------------------------------------------
    charge = data_of(subscription.get("deploy")).get("charge") or {}
    deployed = data_of(subscription.get("deploy")).get("contract") or {}
    want("the subscription program is deployed",
         (subscription.get("deploy") or {}).get("status") == 200
         and bool(subscription.get("contract_id")),
         str((subscription.get("deploy") or {}).get("message")))
    want("the deploy paid the 5 TOR contract price",
         float(charge.get("amount_tor") or 0) == 5.0, str(charge.get("amount_tor")))
    want("the deployed program is the subscription template",
         deployed.get("template") == "subscription", str(deployed.get("template")))
    want("the owner is the hosted wallet",
         deployed.get("owner_address") == address, str(deployed.get("owner_address")))
    subscribed = data_of(subscription.get("subscribe")).get("result")
    renewed = data_of(subscription.get("renew")).get("result")
    expired = data_of(subscription.get("expired")).get("result")
    want("subscribe returned a 30-day expiry",
         isinstance(subscribed, (int, float)) and subscribed > time.time(),
         str(subscribed))
    want("renew extended the expiry",
         isinstance(renewed, (int, float)) and isinstance(subscribed, (int, float))
         and renewed > subscribed, f"{subscribed} -> {renewed}")
    want("expired is false while the period runs", not expired, str(expired))
    read_contract = data_of(subscription.get("read")).get("contract")
    read_contract = read_contract if isinstance(read_contract, dict) else {}
    state = read_contract.get("state")
    state = state if isinstance(state, dict) else {}
    want("the program counted its three calls", int(state.get("calls") or 0) == 3,
         str(state.get("calls")))
    want("the stored expiry is the renewed one",
         int(state.get("expires_at") or 0) == int(renewed or 0),
         f"{state.get('expires_at')} vs {renewed}")
    mine = data_of(subscription.get("mine")).get("contracts") or []
    want("`/api/contracts/mine` lists the program",
         any(row.get("id") == subscription.get("contract_id") for row in mine
             if isinstance(row, dict)))

    # 4c. the NFT transfer -------------------------------------------------
    move = steps.get("nft") or {}
    want("a wrong kind is refused by the NFT door",
         (move.get("wrong_kind") or {}).get("status") == 400,
         str((move.get("wrong_kind") or {}).get("status")))
    moved_by = data_of(move.get("sign"))
    want("the node signs the nft_transfer for its own wallet",
         (move.get("sign") or {}).get("status") == 200
         and bool(moved_by.get("signed_tx")),
         str((move.get("sign") or {}).get("message")))
    receipt = data_of(move.get("transfer")).get("receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    ownership = receipt.get("ownership")
    ownership = ownership if isinstance(ownership, dict) else {}
    want("the move is accepted",
         (move.get("transfer") or {}).get("status") == 200,
         str((move.get("transfer") or {}).get("message")))
    want("the receipt says the receiver owns it now",
         ownership.get("owner_now") == to, str(ownership.get("owner_now")))
    transferred = receipt.get("transfer")
    transferred = transferred if isinstance(transferred, dict) else {}
    want("a public move keeps the sender's address",
         transferred.get("from") == address, str(transferred.get("from")))
    after_move = data_of(move.get("edition_after"))
    want("the ownership door names the new holder", after_move.get("owner") == to,
         str(after_move.get("owner")))
    want("the creator is still the hosted wallet",
         after_move.get("creator") == address, str(after_move.get("creator")))
    theirs = data_of(move.get("holdings_recipient")).get("nfts") or []
    ours = data_of(move.get("holdings_sender")).get("nfts") or []
    want("the receiver's pane carries the edition",
         any(row.get("symbol") == symbol for row in theirs if isinstance(row, dict)))
    want("the sender's pane no longer carries it",
         not any(row.get("symbol") == symbol for row in ours
                 if isinstance(row, dict)))
    history = data_of(move.get("history")).get("records") or []
    first = history[0] if history and isinstance(history[0], dict) else {}
    want("the history names the same move",
         first.get("kind") == "nft_transfer" and first.get("to") == to,
         f"kind={first.get('kind')} to={first.get('to')}")
    shown = data_of(move.get("receipt")).get("transfer")
    shown = shown if isinstance(shown, dict) else {}
    want("the receipt door agrees who received it", shown.get("to") == to,
         str(shown.get("to")))
    want("a replay is 409 NONCE_MISMATCH",
         (move.get("replay") or {}).get("status") == 409
         and (move.get("replay") or {}).get("code") == "NONCE_MISMATCH",
         str((move.get("replay") or {}).get("status")))
    return broken


def transcript(report: dict) -> list[str]:
    """One line per measured fact, so the run reads like a transcript."""
    steps = report["steps"]
    wallet = report.get("wallet") or {}
    opened = steps.get("wallet_opened") or {}
    funding = steps.get("funding") or {}
    edition = steps.get("edition") or {}
    sub = steps.get("subscription") or {}
    nft = steps.get("nft") or {}
    spent = (int(data_of(edition.get("balance_before")).get("balance_micro") or 0)
             - int(data_of(edition.get("balance_after")).get("balance_micro") or 0))
    lines = [
        f"base          {report['base']}",
        f"wallet        {wallet.get('name')}  {wallet.get('address')}",
        f"auth          {opened.get('how')} -> {opened.get('status')}"
        f"  session {bool(opened.get('session'))}"
        f"  one-time key {opened.get('one_time_key_returned')}",
        f"empty         /api/account before funding -> "
        f"{(steps.get('before_funding') or {}).get('status')}",
        f"funding       {funding.get('status')}  {funding.get('amount_tor')} TOR"
        f"  txid {funding.get('txid')}"
        f"  balance {funding.get('balance_micro')} microTOR",
    ]
    for key, row in (steps.get("wallet_doors") or {}).items():
        lines.append(f"wallet door   {key:<26} {row.get('status')}")
    contract = data_of(sub.get("deploy")).get("contract") or {}
    lines += [
        f"token_create  {(edition.get('create') or {}).get('status')}"
        f"  symbol {report['symbol']}  fee {spent // MICRO_PER_TOR} TOR",
        f"edition door  {(edition.get('edition') or {}).get('status')}"
        f"  owner {data_of(edition.get('edition')).get('owner')}",
        f"contract      {(sub.get('deploy') or {}).get('status')}"
        f"  id {sub.get('contract_id')}"
        f"  charge {data_of(sub.get('deploy')).get('charge', {}).get('amount_tor')} TOR",
        f"calls         subscribe={data_of(sub.get('subscribe')).get('result')}"
        f"  renew={data_of(sub.get('renew')).get('result')}"
        f"  expired={data_of(sub.get('expired')).get('result')}"
        f"  calls={data_of(sub.get('read')).get('contract', {}).get('state', {}).get('calls')}",
        f"nft move      {(nft.get('transfer') or {}).get('status')}"
        f"  txid {nft.get('txid')}"
        f"  to {data_of(nft.get('transfer')).get('receipt', {}).get('ownership', {}).get('owner_now')}",
        f"replay        {(nft.get('replay') or {}).get('status')}"
        f"  {(nft.get('replay') or {}).get('code')}",
    ]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="https://www.torblockchain.fun",
                        help="the API to walk (a local stack or a deployed node)")
    parser.add_argument("--funder", default=str(ROOT / "data" / "wallets" / "founder.json"),
                        help="keystore that funds the test wallet (founder by default)")
    parser.add_argument("--fund", type=float, default=18.0,
                        help="TOR sent to the test wallet; 0 skips the funding leg")
    parser.add_argument("--symbol", default="",
                        help="the edition's ticker (2-10 chars; random when omitted)")
    parser.add_argument("--name", default="Hosted wallet probe - native NFT")
    parser.add_argument("--to", default=DEFAULT_RECIPIENT,
                        help="where the edition moves")
    parser.add_argument("--username", default="",
                        help="account username (random when omitted; a re-run logs in)")
    parser.add_argument("--password", default="",
                        help="account password (random when omitted)")
    parser.add_argument("--contract-name", default="Hosted wallet probe - subscription")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="poll this many seconds for the NFT doors to exist")
    parser.add_argument("--out", default="", help="also write the full JSON report here")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    funder = json.loads(Path(args.funder).read_text(encoding="utf-8"))
    symbol = (args.symbol or ("PROBE" + secrets.token_hex(2).upper())).upper()
    username = args.username or ("agent" + secrets.token_hex(3))
    password = args.password or secrets.token_urlsafe(12)
    session = requests.Session()
    report: dict = {
        "base": base, "symbol": symbol, "recipient": args.to,
        "funder": {"address": funder.get("address")}, "fund_tor": args.fund,
        "credentials": {"username": username, "password": password},
        "steps": {},
    }
    steps = report["steps"]

    if args.wait:
        problem = wait_for_doors(session, base, args.wait)
        report["doors_state"] = problem or "live"
        if problem:
            steps["doors"] = survey(session, base)
            if args.out:
                Path(args.out).write_text(json.dumps(report, indent=2),
                                          encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 2

    steps["doors"] = survey(session, base)

    opened = open_wallet(session, base, username, password)
    steps["wallet_opened"] = opened
    report["wallet"] = opened.get("wallet") or {}
    wallet = report["wallet"]
    held = str(opened.get("session") or "")

    # a wallet the node just made has no chain account at all: that 404 is the
    # proof that the funding transfer, not a fixture, created the balance
    steps["before_funding"] = call(session, base, f"/api/account/{wallet['address']}")
    steps["funding"] = fund_wallet(session, base, wallet, funder, args.fund,
                                   "funding a hosted-node test wallet")
    steps["wallet_doors"] = wallet_doors(session, base, wallet, held)
    steps["edition"] = create_edition(session, base, wallet, held, symbol,
                                      args.name)
    steps["subscription"] = deploy_subscription(session, base, wallet, held,
                                                args.contract_name)
    steps["nft"] = move_edition(session, base, wallet, held, symbol, args.to,
                                "native NFT moved by the node's own wallet")

    report["verdict"] = verdict(report)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n".join(transcript(report)))
    if not args.out:
        print(json.dumps(report, indent=2))
    if report["verdict"]:
        print("\nBROKEN PROMISES:", *report["verdict"], sep="\n  - ",
              file=sys.stderr)
        return 1
    print(f"\nevery promise held against {base}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

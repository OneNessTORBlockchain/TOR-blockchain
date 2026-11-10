"""Live smoke test of the new account/ownership endpoints (cleans up after itself)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8090"
TOKEN = "oneness-dev-token"
ROOT = Path(__file__).resolve().parent.parent


def call(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"accept": "application/json",
                                              "content-type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}"
          f"{' -> ' + detail if detail else ''}")
    if not condition:
        sys.exit(1)


# 1. admin status is public and registration is open on a fresh node
status, body = call("GET", "/api/admin/account")
data = body.get("data") or {}
check("admin.account is public", status == 200, f"{status}")
check("registration is open / login closed",
      data.get("registration_open") is True and data.get("registered") is False)
check("support email is support@onenesstech.ca",
      data.get("support_email") == "support@onenesstech.ca")

# 2. weak passwords are refused before any account exists
status, body = call("POST", "/api/admin/account/register",
                    {"username": "operator", "password": "short"})
check("admin register refuses a weak password", status == 400,
      f"{status} {(body.get('error') or {}).get('code')}")
check("no admin account was created", (call("GET", "/api/admin/account")[1]
                                       .get("data") or {}).get("registered") is False)

# 3. login before registration is an honest 404 with the fix
status, body = call("POST", "/api/admin/account/login",
                    {"username": "operator", "password": "a good password"})
check("admin login before registration -> 404", status == 404,
      (body.get("error") or {}).get("message", ""))

# 4. pricing states the fee split
status, body = call("GET", "/api/pricing")
fees = (body.get("data") or {}).get("fees") or {}
rows = {row["code"]: row for row in (body.get("data") or {}).get("rows", [])}
check("base layer fee is 0", fees.get("base_layer_tx_fee_micro") == 0,
      str(fees.get("base_layer_tx_fee_tor")))
check("node/platform transfer row is 0.1 TOR",
      rows.get("transfer", {}).get("tor") == 0.1)

# 5. the live node accepts a fee-receiver change (founder -> founder = no-op)
status, body = call("GET", "/api/chain/info")
founder = (body.get("data") or {}).get("founder")
status, body = call("POST", "/api/admin/fee-receiver", {"address": founder},
                    {"x-admin-token": TOKEN})
check("fee-receiver route answers", status == 200 and
      (body.get("data") or {}).get("fee_receiver") == founder, f"{status}")

# 6. username signup -> one-time key -> session -> own wallet only
status, body = call("POST", "/api/auth/signup",
                    {"username": "smoke-test", "password": "smoke-test-password"})
check("wallet signup created a real wallet", status == 200, f"{status}")
created = body.get("data") or {}
one_time = created.get("one_time") or {}
wallet = created.get("wallet") or {}
session = (created.get("session") or {}).get("session")
check("private key returned once", bool(one_time.get("private_key_base58")) and
      one_time.get("shown_once") is True)
check("wallet address is TOR1...onion",
      str(wallet.get("address", "")).startswith("TOR1") and
      str(wallet.get("address", "")).endswith("onion"))
status, body = call("GET", "/api/auth/me", headers={"x-wallet-session": session})
check("session reads its own wallet", status == 200 and
      (body.get("data") or {}).get("wallet", {}).get("name") == wallet.get("name"))
status, body = call("GET", "/api/auth/me")
check("no session -> 403", status == 403)
status, body = call("GET", f"/api/wallets/{wallet.get('name')}/balance",
                    headers={"x-wallet-session": session})
check("session reads its own balance", status == 200)
status, body = call("GET", "/api/wallets/founder/balance",
                    headers={"x-wallet-session": session})
check("another wallet's balance is refused", status == 403)

# 7. login again never re-shows the key
status, body = call("POST", "/api/auth/login",
                    {"username": "smoke-test", "password": "smoke-test-password"})
relogin = body.get("data") or {}
check("login works and never returns the key",
      status == 200 and "one_time" not in relogin)

# 8. ownership: a stranger sees a summary, the owner sees everything
status, body = call("GET", "/api/contracts")
listed = (body.get("data") or {}).get("contracts") or []
check("contracts list answers", status == 200, f"{len(listed)} row(s)")
if listed:
    contract_id = listed[0]["id"]
    status, body = call("GET", f"/api/contracts/{contract_id}")
    check("anonymous view is a summary", status == 200 and
          (body.get("data") or {}).get("contract", {}).get("access") == "summary")
    status, body = call("GET", f"/api/contracts/{contract_id}",
                        headers={"x-wallet-session": session})
    check("stranger with a session still gets a summary", status == 200 and
          (body.get("data") or {}).get("contract", {}).get("access") == "summary")
    status, body = call("POST", f"/api/contracts/{contract_id}/call",
                        {"function": "status"},
                        headers={"x-wallet-session": session})
    check("stranger cannot manage it", status == 403)

# 9. logout closes the session
status, body = call("POST", "/api/auth/logout", {},
                    {"x-wallet-session": session})
check("logout works", status == 200 and
      (body.get("data") or {}).get("logged_out") is True)

# ---------------------------------------------------------------- clean up
sys.path.insert(0, str(ROOT))
from oneness_tor.services import accounts  # noqa: E402

row = accounts.find_username("smoke-test")
if row:
    accounts.ACCOUNTS.delete(str(row["id"]))
wallet_name = str(wallet.get("name") or "")
if wallet_name:
    (ROOT / "data" / "wallets" / f"{wallet_name}.json").unlink(missing_ok=True)
check("smoke account removed again", accounts.find_username("smoke-test") is None)
print("\nall live account/ownership checks passed")

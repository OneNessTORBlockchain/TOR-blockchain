"""Live proof of both builder deployments (cleans up its own records)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8090"
TOKEN = "oneness-dev-token"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def call(method: str, path: str, body=None, headers=None, timeout=900):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"accept": "application/json",
                                              "content-type": "application/json",
                                              "x-admin-token": TOKEN,
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}"
          f"{' -> ' + detail if detail else ''}")
    if not condition:
        sys.exit(1)


status, body = call("GET", "/api/workbench/projects")
projects = (body.get("data") or {}).get("projects") or []
rust = next((row for row in projects if row.get("kind") == "rust"), None)
created_here = False
if rust is None:
    status, body = call("POST", "/api/workbench/projects",
                        {"name": "live rust proof", "kind": "rust"})
    rust = (body.get("data") or {}).get("project") or {}
    created_here = True
check("a Rust project exists", bool(rust.get("id")), str(rust.get("id")))

status, body = call("POST", f"/api/workbench/projects/{rust['id']}/deploy",
                    {"owner_wallet": "founder"})
check("rust deploy compiled the program", status == 200,
      f"{status} {(body.get('error') or {}).get('message', '')[:160]}")
data = body.get("data") or {}
deployment = data.get("deployment") or {}
program = data.get("program") or {}
check("deployment carries the program id", bool(deployment.get("program_id")),
      str(deployment.get("program_id")))
check("binary sha3 is recorded", len(str(deployment.get("binary_sha3"))) == 64)
check("owner is the deploying wallet", data.get("owner_address") ==
      (call("GET", f"/api/contracts/{deployment.get('program_id')}")[1]
       .get("data", {}).get("contract", {}).get("owner_address")))
check("binary exists on disk", Path(str(deployment.get("binary"))).is_file(),
      str(deployment.get("binary")))

status, body = call("GET", f"/api/contracts/{deployment['program_id']}",
                    headers={"x-admin-token": ""})
check("anonymous view is a summary",
      (body.get("data") or {}).get("contract", {}).get("access") == "summary")
status, body = call("GET", f"/api/contracts/{deployment['program_id']}")
full = (body.get("data") or {}).get("contract") or {}
check("owner/admin sees the source", bool(full.get("source")) and
      full.get("access") in ("owner", "admin"))

# DarkWeb: no Tor daemon in this environment -> the honest error, never a fake onion
status, body = call("POST", "/api/workbench/projects",
                    {"name": "live web proof", "kind": "web"})
web = (body.get("data") or {}).get("project") or {}
status, body = call("POST", f"/api/workbench/projects/{web['id']}/deploy", {})
error = body.get("error") or {}
check("DarkWeb deploy without Tor is an honest TOR_DAEMON_UNAVAILABLE",
      status >= 400 and error.get("code") == "TOR_DAEMON_UNAVAILABLE",
      error.get("message", "")[:110])

# ------------------------------------------------------------------ cleanup
from oneness_tor.services import contracts, workbench  # noqa: E402

if deployment.get("program_id"):
    try:
        contracts.CONTRACTS.delete(str(deployment["program_id"]))
    except Exception:  # noqa: BLE001 - already gone
        pass
for build_id in (web.get("id"), rust.get("id") if created_here else None):
    if not build_id:
        continue
    try:
        workbench.delete_project(str(build_id))
    except Exception:  # noqa: BLE001 - already gone
        pass
print("\nlive builder checks passed (test records removed)")

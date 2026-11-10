"""Rust smart-contract registry.

Deployment is restricted to authorized nodes (admin token) and costs 5 TOR,
charged on-chain *before* the contract is stored. The source is hashed and, when
``rustc`` is available, actually compiled with ``--emit=metadata`` so a broken
contract is rejected instead of stored.

Scope, stated honestly: this layer stores contract source + its key/value state
and settles every fee on the Rust ledger. The Rust chain itself does not execute
WASM yet (see ``rust/torchain/README.md`` → "Honest limits").
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.errors import ValidationError
from core.node_client import node
from oneness_tor.services import ledger
from oneness_tor.services.store import Collection, data_dir, read_json, write_json

CONTRACTS = Collection(lambda: data_dir("contracts") / "contracts.json", "ctr")


def nodes_path():
    return data_dir("contracts") / "nodes.json"

TEMPLATES: dict[str, dict] = {
    "escrow": {
        "name": "Escrow",
        "description": "Buyer/seller escrow: deposit, release, refund.",
        "functions": ["deposit", "release", "refund", "status"],
        "source": """//! OneNess TOR — escrow contract (no external crates)
pub struct Escrow {
    pub buyer: String,
    pub seller: String,
    pub amount_micro: u64,
    pub released: bool,
}

impl Escrow {
    pub fn new(buyer: &str, seller: &str, amount_micro: u64) -> Self {
        Self { buyer: buyer.to_string(), seller: seller.to_string(),
               amount_micro, released: false }
    }
    pub fn deposit(&mut self, amount_micro: u64) -> u64 {
        self.amount_micro += amount_micro;
        self.amount_micro
    }
    pub fn release(&mut self) -> u64 {
        self.released = true;
        self.amount_micro
    }
    pub fn refund(&mut self) -> u64 {
        self.released = false;
        0
    }
    pub fn status(&self, _unused: u64) -> u64 {
        if self.released { 1 } else { 0 }
    }
}

pub fn call(state: &mut Escrow, function: &str, amount_micro: u64) -> u64 {
    match function {
        "deposit" => state.deposit(amount_micro),
        "release" => state.release(),
        "refund" => state.refund(),
        _ => state.status(0),
    }
}
"""},
    "token_gate": {
        "name": "Token gate",
        "description": "Grants access while the caller holds at least N tokens.",
        "functions": ["open", "close", "check"],
        "source": """//! OneNess TOR — token gate contract (no external crates)
pub struct TokenGate {
    pub symbol: String,
    pub minimum: u64,
    pub open: bool,
}

impl TokenGate {
    pub fn new(symbol: &str, minimum: u64) -> Self {
        Self { symbol: symbol.to_string(), minimum, open: true }
    }
    pub fn check(&self, holding: u64) -> bool { self.open && holding >= self.minimum }
    pub fn open(&mut self, _unused: u64) -> u64 { self.open = true; 1 }
    pub fn close(&mut self, _unused: u64) -> u64 { self.open = false; 0 }
}

pub fn call(state: &mut TokenGate, function: &str, holding: u64) -> u64 {
    match function {
        "check" => u64::from(state.check(holding)),
        "open" => state.open(0),
        "close" => state.close(0),
        _ => u64::from(state.check(holding)),
    }
}
"""},
    "subscription": {
        "name": "Subscription",
        "description": "30-day service subscription with renewal and expiry.",
        "functions": ["subscribe", "renew", "expired"],
        "source": """//! OneNess TOR — subscription contract (no external crates)
pub struct Subscription {
    pub owner: String,
    pub expires_at: i64,
    pub days: u64,
}

impl Subscription {
    pub fn new(owner: &str, days: u64, now: i64) -> Self {
        Self { owner: owner.to_string(), days, expires_at: now + (days as i64) * 86_400 }
    }
    pub fn subscribe(&mut self, now: i64) -> i64 {
        self.expires_at = now + (self.days as i64) * 86_400;
        self.expires_at
    }
    pub fn renew(&mut self, now: i64) -> i64 {
        self.expires_at = self.expires_at.max(now) + (self.days as i64) * 86_400;
        self.expires_at
    }
    pub fn expired(&self, now: i64) -> u64 { u64::from(now > self.expires_at) }
}

pub fn call(state: &mut Subscription, function: &str, now: i64) -> i64 {
    match function {
        "subscribe" => state.subscribe(now),
        "renew" => state.renew(now),
        "expired" => state.expired(now) as i64,
        _ => state.expired(now) as i64,
    }
}
"""},
}

ALLOWED_STATE = {"deposit", "release", "refund", "status", "open", "close",
                 "check", "subscribe", "renew", "expired", "run"}

#: What a non-owner may see. Source, live state, compiler output, fees and the
#: binary path stay with the program's owner (or an admin credential).
SUMMARY_FIELDS = ("id", "kind", "name", "template", "owner_address", "status",
                  "source_sha3", "binary_sha3", "functions", "build_id",
                  "created_at", "created_unix")


def find_rustc() -> str | None:
    """``rustc`` from PATH or the usual rustup install locations."""
    found = shutil.which("rustc")
    if found:
        return found
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin" / "rustc.exe",
        Path(os.environ.get("HOME", "")) / ".cargo" / "bin" / "rustc",
        Path("/usr/local/bin/rustc"), Path("/usr/bin/rustc"),
        Path("/opt/homebrew/bin/rustc"),
    ]
    for path in candidates:
        try:
            if str(path) and path.exists():
                return str(path)
        except OSError:
            continue
    return None


def templates() -> list[dict]:
    return [{"id": key, "name": value["name"],
             "description": value["description"],
             "functions": value["functions"],
             "source_bytes": len(value["source"].encode("utf-8"))}
            for key, value in TEMPLATES.items()]


def compile_check(source: str) -> dict:
    """Compile the contract with ``rustc --emit=metadata`` when available."""
    rustc = find_rustc()
    if not rustc:
        return {"checked": False, "ok": None, "compiler": None,
                "note": "rustc not found (PATH, ~/.cargo/bin) — source hashed only"}
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="tor-contract-") as folder:
        source_path = Path(folder) / "contract.rs"
        source_path.write_text(source, encoding="utf-8")
        try:
            result = subprocess.run(
                [rustc, "--edition", "2021", "--crate-type", "lib",
                 "--emit=metadata", "-o", str(Path(folder) / "contract.rmeta"),
                 str(source_path)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180)
        except (OSError, subprocess.SubprocessError) as problem:
            return {"checked": True, "ok": False, "compiler": rustc,
                    "stderr": str(problem)[:800],
                    "ms": int((time.time() - started) * 1000)}
    return {"checked": True, "ok": result.returncode == 0, "compiler": rustc,
            "stderr": (result.stderr or "")[-1200:],
            "ms": int((time.time() - started) * 1000)}


def authorized_nodes() -> list[dict]:
    """The nodes allowed to deploy: this node plus anything in nodes.json."""
    base: list[dict] = []
    try:
        info = node.info().get("data") or {}
    except Exception:
        info = {}
    founder = str(info.get("founder") or "")
    if founder:
        base.append({"name": "primary", "address": founder, "authorized": True,
                     "fingerprint": hashlib.sha256(founder.encode()).hexdigest()[:12]})
    extra = read_json(nodes_path(), []) or []
    seen = {row["address"] for row in base}
    for row in extra if isinstance(extra, list) else []:
        address = str(row.get("address") or "")
        if address and address not in seen:
            base.append({"name": row.get("name") or "peer", "address": address,
                         "authorized": bool(row.get("authorized", True)),
                         "fingerprint": hashlib.sha256(
                             address.encode()).hexdigest()[:12]})
            seen.add(address)
    return base


def authorize_node(name: str, address: str, authorized: bool = True) -> dict:
    from core.validator import validate_address

    validate_address(address, "address")
    rows = read_json(nodes_path(), []) or []
    rows = [row for row in rows if row.get("address") != address]
    rows.append({"name": str(name or "peer")[:48], "address": address,
                 "authorized": bool(authorized),
                 "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    write_json(nodes_path(), rows)
    return {"nodes": authorized_nodes()}


def summary(row: dict) -> dict:
    """The public slice of a program: identity + hashes, never its innards."""
    return {key: row[key] for key in SUMMARY_FIELDS if key in row}


def is_owner(row: dict, address: str | None) -> bool:
    wanted = str(address or "").strip()
    return bool(wanted) and str(row.get("owner_address") or "") == wanted


def view(row: dict, *, viewer_address: str | None = None,
         admin: bool = False) -> dict:
    """Full record for the owner/admin, a summary for everybody else."""
    if admin or is_owner(row, viewer_address):
        body = dict(row)
        body["access"] = "admin" if admin and not is_owner(row, viewer_address) \
            else "owner"
        return body
    body = summary(row)
    body["access"] = "summary"
    body["restricted"] = ("only the program owner can view the source, state, "
                          "compiler output and fee history")
    return body


def list_contracts(limit: int = 50, *, viewer_address: str | None = None,
                   admin: bool = False) -> dict:
    rows = CONTRACTS.all()
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    rows = [view(row, viewer_address=viewer_address, admin=admin)
            for row in rows[:max(1, int(limit))]]
    return {"contracts": rows, "count": len(CONTRACTS.all()),
            "nodes": authorized_nodes()}


def list_mine(address: str, limit: int = 50) -> dict:
    rows = [row for row in CONTRACTS.all() if is_owner(row, address)]
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    return {"contracts": rows[:max(1, int(limit))], "count": len(rows),
            "owner_address": str(address)}


def get_contract(contract_id: str, *, viewer_address: str | None = None,
                 admin: bool = False) -> dict:
    return view(CONTRACTS.get(contract_id), viewer_address=viewer_address,
                admin=admin)


def require_owner(contract_id: str, *, viewer_address: str | None = None,
                  admin: bool = False) -> dict:
    """Full record, or 403 — the owner manages the program, nobody else."""
    row = CONTRACTS.get(contract_id)
    if admin or is_owner(row, viewer_address):
        return row
    from core.errors import Forbidden

    raise Forbidden(
        "only the program owner can manage this program — sign in with the "
        "wallet that deployed it",
        details={"contract": str(contract_id),
                 "owner_address": row.get("owner_address"),
                 "header": "x-wallet-session"})


def deploy(*, name: str, template: str | None = None, source: str | None = None,
           owner_wallet: str = "founder", wallet: str = "founder") -> dict:
    """Compile → charge 5 TOR on-chain → store the contract."""
    chosen = str(template or "").strip().lower()
    if chosen and chosen not in TEMPLATES:
        raise ValidationError(f"unknown template {chosen!r}",
                              details={"templates": sorted(TEMPLATES)})
    code = source or (TEMPLATES[chosen]["source"] if chosen else "")
    if not str(code).strip():
        raise ValidationError("provide a template id or the Rust source")
    if "fn call" not in code:
        raise ValidationError("a contract must expose `pub fn call(...)`")
    compiled = compile_check(code)
    if compiled.get("ok") is False:
        raise ValidationError("the contract does not compile",
                              details={"stderr": compiled.get("stderr", "")[-900:]})
    charge = ledger.charge(wallet, "contract_create",
                           note=f"{name} {chosen or 'custom'}")
    owner = ledger.load_wallet(owner_wallet)
    contract = CONTRACTS.add({
        "kind": "contract",
        "name": str(name or "contract")[:64], "template": chosen or "custom",
        "owner": owner_wallet, "owner_address": owner.get("address"),
        "deployer": ledger.load_wallet(wallet).get("address"),
        "source": code,
        "source_sha3": hashlib.sha3_256(code.encode("utf-8")).hexdigest(),
        "status": "deployed", "compile": compiled,
        "functions": TEMPLATES.get(chosen, {}).get(
            "functions", ["deposit", "release", "status"]),
        "state": {"calls": 0},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_unix": int(time.time()), "txid": charge.get("txid"),
        "charge": {"code": charge["code"], "amount_micro": charge["amount_micro"],
                   "amount_tor": charge["amount_tor"]}})
    return {"contract": contract, "txid": charge.get("txid"), "charge": charge,
            "authorized_nodes": authorized_nodes()}


def deploy_program(*, build_id: str, name: str, wallet: str, source: str,
                   binary_path: str, binary_sha3: str,
                   compile: dict | None = None, crate: str = "") -> dict:
    """Register a compiled Rust-builder program. The owner manages it."""
    owner = ledger.load_wallet(wallet)
    address = owner.get("address")
    return CONTRACTS.add({
        "kind": "rust_program", "name": str(name or build_id)[:64],
        "template": "rust_program", "crate": str(crate or ""),
        "build_id": str(build_id), "owner": wallet, "owner_address": address,
        "deployer": address, "source": source,
        "source_sha3": hashlib.sha3_256(source.encode("utf-8")).hexdigest(),
        "binary_path": str(binary_path), "binary_sha3": str(binary_sha3),
        "status": "deployed", "compile": compile or {"checked": True, "ok": True},
        "functions": ["run", "status"], "state": {"runs": 0},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_unix": int(time.time()), "txid": None, "charge": None,
        "note": ("deployed from the Rust Program Builder; no chain fee — the "
                 "base layer is free and this is an operator deployment")})


def call(contract_id: str, function: str, args: dict | None = None, *,
         actor_address: str | None = None, admin: bool = False) -> dict:
    """Apply a function to the stored contract state (deterministic, audited)."""
    contract = require_owner(contract_id, viewer_address=actor_address,
                             admin=admin)
    requested = str(function or "").strip()
    if requested not in ALLOWED_STATE:
        raise ValidationError(f"unknown function {requested!r}",
                              details={"allowed": sorted(ALLOWED_STATE)})
    if requested not in contract.get("functions", ALLOWED_STATE):
        raise ValidationError(f"{requested} is not part of this contract",
                              details={"functions": contract.get("functions")})
    args = args if isinstance(args, dict) else {}
    state = dict(contract.get("state") or {})
    now = int(time.time())
    amount_micro = int(args.get("amount_micro") or 0)
    if not amount_micro and args.get("amount_tor"):
        amount_micro = int(round(float(args["amount_tor"]) * 10 ** 8))
    holding = int(args.get("holding") or args.get("balance") or 0)
    result: object
    if requested == "deposit":
        if amount_micro <= 0:
            raise ValidationError("deposit needs amount_micro (or amount_tor)")
        state["amount_micro"] = int(state.get("amount_micro") or 0) + amount_micro
        result = state["amount_micro"]
    elif requested == "release":
        if not state.get("amount_micro"):
            raise ValidationError("nothing is deposited in this escrow")
        state["released"] = True
        result = state["amount_micro"]
    elif requested == "refund":
        state["released"] = False
        result = 0
    elif requested == "status":
        result = 1 if state.get("released") else 0
    elif requested == "check":
        result = bool(state.get("open", True) and holding >= int(state.get("minimum") or 0))
    elif requested in ("open", "close"):
        state["open"] = requested == "open"
        result = state["open"]
    elif requested == "subscribe":
        days = int(state.get("days") or args.get("days") or 30)
        state["days"] = days
        state["expires_at"] = now + days * 86_400
        result = state["expires_at"]
    elif requested == "renew":
        days = int(state.get("days") or args.get("days") or 30)
        state["expires_at"] = max(int(state.get("expires_at") or 0), now) + days * 86_400
        result = state["expires_at"]
    elif requested == "run":
        if str(contract.get("kind")) != "rust_program":
            raise ValidationError("only a deployed Rust program can be run")
        from oneness_tor.services import workbench

        executed = workbench.run_program(
            str(contract.get("build_id")),
            timeout=float(args.get("timeout") or 60))
        state["runs"] = int(state.get("runs") or 0) + 1
        state["last_run"] = {"at": now, "exit_code": executed.get("exit_code"),
                             "ok": executed.get("ok"),
                             "stdout": (executed.get("stdout") or "")[-4000:],
                             "stderr": (executed.get("stderr") or "")[-2000:]}
        result = {"runs": state["runs"], "exit_code": executed.get("exit_code"),
                  "ok": executed.get("ok"), "stdout": state["last_run"]["stdout"],
                  "stderr": state["last_run"]["stderr"]}
    else:  # expired
        result = now > int(state.get("expires_at") or 0)
    state["calls"] = int(state.get("calls") or 0) + 1
    state["last_call"] = {"function": requested, "at": now,
                          "by": str(actor_address or ("admin" if admin else "")),
                          "args": args, "result": result}
    updated = CONTRACTS.update(contract_id, state=state)
    return {"contract": updated, "result": result, "state": state, "at": now}

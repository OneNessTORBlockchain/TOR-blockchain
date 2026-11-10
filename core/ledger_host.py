"""Host the Rust ledger next to the API when both live on the same machine.

Every Python surface reads the chain through :mod:`core.node_client`, so the API
is only as useful as the node it points at. On a laptop that node is usually
already running (``scripts/run_node.ps1``); on Render, Docker or a VPS the
container holds *both* processes, and nothing else will ever start the ledger —
which is exactly how a deployment ends up answering ``503 CHAIN_UNAVAILABLE`` on
every chain route while ``/api/health`` still says the web service is up.

:func:`ensure_local_node` closes that gap, and it is deliberately conservative:

* it only acts when ``node.api_url`` is **loopback** — a remote node is somebody
  else's process, never ours to spawn;
* it never competes with a running node: a successful ``GET /health`` returns
  immediately and leaves it alone;
* it refuses to invent a chain: a fresh container has no ``state.json``, so a
  *verified* snapshot must be supplied (``ONENESS_NODE_SEED``, or
  ``seed/chain-snapshot.json`` built by ``scripts/export_seed.py``). The snapshot
  is re-hashed by :func:`oneness_tor.services.node_sync.verify_snapshot` before a
  single byte is written, and only the node-local ``admin_token_sha256`` is
  replaced with this deployment's own token (it is not part of ``state_root``);
* every outcome is returned as data — ``started``, ``reachable``, ``hint``,
  ``error`` — so the banner and ``/api/health`` can print the truth instead of a
  green light that means nothing.

    from core import ledger_host
    ledger = ledger_host.ensure_local_node(wait=25)
    ledger["started"], ledger["reachable"]      # True, True   (or a real hint)
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from core.config import ROOT, settings
from core.errors import AppError, ChainUnavailable, ValidationError
from core.node_client import NodeClient

ENV_AUTOSTART = "ONENESS_NODE_AUTOSTART"
ENV_BINARY = "ONENESS_NODE_BINARY"
ENV_SEED = "ONENESS_NODE_SEED"

#: Committed (or operator-mounted) snapshot used to give a fresh container the
#: chain it is supposed to serve. ``scripts/export_seed.py`` writes this file.
DEFAULT_SEED = "seed/chain-snapshot.json"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""}

_PROCESS: subprocess.Popen | None = None
_LAST: dict = {"managed": False, "pid": None, "started": False,
               "reachable": None, "binary": None, "seed": None,
               "chain_dir": None, "hint": "the start command has not run yet"}


# ------------------------------------------------------------------ addressing
def is_loopback(host: str) -> bool:
    return str(host or "").strip().lower() in LOOPBACK


def bind_address(url: str | None = None) -> str | None:
    """``host:port`` for a loopback ``node.api_url``, else ``None`` (remote node)."""
    text = str(url or settings.node_api_url).strip()
    parts = urllib.parse.urlsplit(text if "://" in text else "http://" + text)
    host = (parts.hostname or "").lower()
    if not is_loopback(host):
        return None
    if host in ("0.0.0.0", "::", "", "localhost", "::1"):
        host = "127.0.0.1"            # `torchain serve` binds IPv4 loopback here
    return f"{host}:{parts.port or 8545}"


def node_url() -> str:
    return settings.node_api_url


# ---------------------------------------------------------------------- chain
def chain_dir() -> Path:
    return settings.chain_dir


def chain_ready() -> bool:
    return (chain_dir() / "state.json").is_file()


# -------------------------------------------------------------------- process
def find_binary() -> str | None:
    """``ONENESS_NODE_BINARY`` → ``node.binary`` → repo build → ``PATH`` → ``bin/torchain``.

    A binary *this* host compiled always beats the artifact committed in ``bin/``.
    That file is the prebuilt Linux binary shipped for hosts that cannot build
    Rust at all (a native Render service runs ``pip install`` and nothing else),
    so it is the last resort: a Docker image compiles the ledger into
    ``/usr/local/bin/torchain`` *and* copies the checkout, and the committed copy
    can be older than the seed it has to read — which is exactly how the live
    service died on ``error [BAD_JSON] state.json is corrupt: missing field
    `public_until``` while a fresh ledger sat unused on ``PATH``. A compiled
    binary matches the source tree by construction; a committed one cannot
    promise that.
    """
    explicit = str(os.environ.get(ENV_BINARY)
                   or settings.get("node.binary") or "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT / path
        if os.name == "nt" and path.suffix.lower() != ".exe":
            path = path.with_suffix(".exe")
        if path.is_file():
            return str(path)
    configured = settings.chain_binary
    if configured.is_file():
        return str(configured)
    on_path = shutil.which("torchain")
    if on_path:
        return on_path
    shipped = ROOT / "bin" / ("torchain.exe" if os.name == "nt" else "torchain")
    if shipped.is_file():
        return str(shipped)
    return None


def command_for(binary: str, bind: str) -> list[str]:
    """Exactly what ``scripts/run_node.ps1`` runs, for any platform."""
    return [binary, "serve", "--dir", str(chain_dir()), "--bind", bind]


def _probe() -> bool:
    """One short ``GET /health`` against the node this process is configured for."""
    try:
        return NodeClient(settings.node_api_url, timeout=2.0).is_reachable()
    except (ChainUnavailable, ValueError, OSError):
        return False


def _die_with_parent() -> None:
    """Linux: SIGTERM the ledger when this process dies (``PR_SET_PDEATHSIG``).

    Render/Docker restarts and crashes are SIGKILL far too often for an ``atexit``
    hook to run; without this the orphan keeps the chain directory and port
    8545, so the next boot attaches to a process it did not start.
    """
    try:
        import ctypes
        import signal as signal_module

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal_module.SIGTERM)
    except Exception:      # noqa: BLE001 - best effort, never fatal
        return


def _spawn(binary: str, bind: str) -> subprocess.Popen:
    return subprocess.Popen(
        command_for(binary, bind), cwd=str(ROOT),
        stdout=sys.stdout, stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)


def stop() -> dict:
    """Stop the child this process started (a no-op for anybody else's node)."""
    global _PROCESS
    process, _PROCESS = _PROCESS, None
    if process is None or process.poll() is not None:
        return {"stopped": False, "reason": "no ledger process was started here"}
    try:
        process.terminate()
    except OSError:
        return {"stopped": False, "reason": "the ledger process already exited"}
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
    return {"stopped": True, "pid": process.pid}


# ----------------------------------------------------------------------- seed
def seed_source() -> str:
    """The snapshot a fresh node may adopt: env var first, then the repo file."""
    explicit = str(os.environ.get(ENV_SEED)
                   or settings.get("node.seed") or "").strip()
    if explicit:
        return explicit
    default = ROOT / DEFAULT_SEED
    return str(default) if default.is_file() else ""


def load_seed(source: str) -> dict:
    """Read a snapshot from a path or an ``http(s)`` URL (a verified envelope)."""
    text = str(source or "").strip()
    if not text:
        raise ValidationError("a ledger snapshot path or URL is required")
    if text.startswith(("http://", "https://")):
        headers = {"accept": "application/json"}
        token = str(settings.get("sync.origin_token") or "").strip()
        if token:
            headers["x-admin-token"] = token
        request = urllib.request.Request(text, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as reply:
            payload = json.loads(reply.read().decode("utf-8", "replace"))
    else:
        path = Path(text)
        if not path.is_absolute():
            path = ROOT / path
        payload = json.loads(path.read_text(encoding="utf-8"))
    if (isinstance(payload, dict) and isinstance(payload.get("data"), dict)
            and "state" not in payload):
        payload = payload["data"]           # GET /api/chain/sync/snapshot envelope
    if not isinstance(payload, dict):
        raise ValidationError(
            f"{text} is not a ledger snapshot (expected a JSON object)")
    return payload


def write_chain(snapshot: dict, *, target: Path | None = None,
                admin_token: str | None = None) -> dict:
    """Verify a snapshot, then write ``state.json`` + ``blocks.jsonl``.

    Verification happens *before* anything touches the directory, so a corrupt
    or hostile snapshot can never leave a half-ledger behind. The local
    ``admin_token_sha256`` is preserved (or set from this deployment's token):
    node authentication is not part of ``state_root``, and a seeded container
    must keep answering to *its own* admin token.
    """
    from oneness_tor.services import node_sync   # late: keep core import-light

    verified = node_sync.verify_snapshot(snapshot)
    state = dict(verified["state"])
    token = str(admin_token if admin_token is not None
                else settings.admin_token or "").strip()
    if token and token != "change-me":
        state["admin_token_sha256"] = hashlib.sha3_256(
            token.encode("utf-8")).hexdigest()

    by_height: dict[int, list] = {}
    for record in state.get("records") or []:
        if isinstance(record, dict):
            by_height.setdefault(int(record.get("height") or 0), []).append(record)
    lines = "".join(
        json.dumps({"block": block,
                    "txs": by_height.get(int(block.get("height") or 0), [])},
                   ensure_ascii=False, separators=(",", ":")) + "\n"
        for block in verified["blocks"])

    directory = target or chain_dir()
    directory.mkdir(parents=True, exist_ok=True)
    blocks_temp = directory / "blocks.jsonl.tmp"
    state_temp = directory / "state.json.tmp"
    blocks_temp.write_text(lines, encoding="utf-8")
    state_temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    os.replace(blocks_temp, directory / "blocks.jsonl")
    os.replace(state_temp, directory / "state.json")
    return {"height": verified["height"], "head_hash": verified["head_hash"],
            "genesis_hash": verified["genesis_hash"],
            "tokens": verified["tokens"], "accounts": verified["accounts"],
            "records": verified["records"], "chain_dir": str(directory),
            "admin_token_kept": bool(token and token != "change-me")}


def seed_chain(source: str) -> dict:
    summary = write_chain(load_seed(source))
    summary["source"] = source
    return summary


# ------------------------------------------------------------------- lifecycle
def ensure_local_node(*, autostart: bool | None = None,
                      wait: float = 25.0) -> dict:
    """Start the ledger for a loopback ``node.api_url`` and report what happened."""
    global _PROCESS, _LAST
    result: dict = {"url": node_url(), "reachable": False, "managed": False,
                    "started": False, "pid": None, "binary": None,
                    "chain_dir": str(chain_dir()), "seed": None,
                    "error": None, "hint": None}
    bind = bind_address()
    if bind is None:
        result["reachable"] = _probe()
        result["hint"] = (f"node.api_url is {result['url']} — another host, so this "
                          "process has nothing to start")
        _LAST = result
        return result

    if _probe():
        result.update(reachable=True, hint="already serving — left it alone")
        _LAST = result
        return result

    if autostart is False or not bool(settings.get("node.autostart")):
        result["hint"] = ("autostart is off (node.autostart / "
                          f"{ENV_AUTOSTART}=0) — run `torchain serve --bind {bind}`")
        _LAST = result
        return result

    binary = find_binary()
    result["binary"] = binary
    if not binary:
        result["hint"] = ("torchain is not built in this checkout — `cargo build "
                          "--release --manifest-path rust/torchain/Cargo.toml`, or "
                          f"set {ENV_BINARY} to the binary")
        _LAST = result
        return result

    if not chain_ready():
        source = seed_source()
        if not source:
            result["hint"] = (f"no chain at {result['chain_dir']} — supply a verified "
                              f"snapshot with {ENV_SEED} (path or https URL) or run "
                              "`torchain init --founder <wallet.json>` once")
            _LAST = result
            return result
        try:
            result["seed"] = seed_chain(source)
        except AppError as problem:
            result["error"] = problem.message
            _LAST = result
            return result
        except (OSError, ValueError) as problem:
            result["error"] = f"seed {source} could not be adopted: {problem}"
            _LAST = result
            return result

    process = _spawn(binary, bind)
    _PROCESS = process
    deadline = time.monotonic() + max(1.0, float(wait))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _PROCESS = None
            result["error"] = (f"torchain exited with code {process.returncode} — "
                               f"`{binary} serve --bind {bind} --dir "
                               f"{result['chain_dir']}` prints the reason")
            _LAST = result
            return result
        if _probe():
            result.update(reachable=True, managed=True, started=True,
                          pid=process.pid)
            atexit.register(stop)
            _LAST = result
            return result
        time.sleep(0.2)
    result.update(managed=True, started=True, pid=process.pid,
                  hint=(f"torchain started (pid {process.pid}) but {result['url']} "
                        f"did not answer /health in {float(wait):.0f}s"))
    atexit.register(stop)
    _LAST = result
    return result


def status() -> dict:
    """What this process knows about the ledger — no network, safe for /api/health."""
    process = _PROCESS
    return {**_LAST, "managed": process is not None,
            "pid": process.pid if process is not None and process.poll() is None
            else _LAST.get("pid"),
            "alive": bool(process is not None and process.poll() is None),
            "binary": _LAST.get("binary") or find_binary(),
            "chain_dir": str(chain_dir()), "chain_ready": chain_ready(),
            "seed_source": seed_source() or None}

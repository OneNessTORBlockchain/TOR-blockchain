"""Tor daemon control — real ``ADD_ONION`` / ``SIGNAL NEWNYM`` or an honest error.

A hidden service is only reported after the control port actually answered
``250 OK``; if Tor is missing or the control port is not enabled the API raises
``TOR_DAEMON_UNAVAILABLE`` with the exact fix (never a made-up .onion address).

The start command can also *run* Tor (``tor.auto_start`` / ``ONENESS_TOR_AUTOSTART``):
the torrc written here opens the SOCKS and control ports with cookie
authentication, and the daemon is spawned detached so the API outlives the
request that started it. That is what makes a Render/Docker deploy publish an
onion address without anyone shelling into the container.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.errors import TorUnavailable, ValidationError
from oneness_tor.services.store import Collection, data_dir

SERVICES = Collection(lambda: data_dir("tor") / "services.json", "onion")
PROTOCOL = "tor-service-v1"


def _host() -> str:
    return str(settings.get("tor.hostname") or "127.0.0.1")


def _control_port() -> int:
    return int(settings.get("tor.control_port") or 9051)


def binary() -> str | None:
    """The ``tor`` executable: ``tor.binary`` on PATH, else a bundled runtime.

    ``data/tor/runtime/tor/tor(.exe)`` is where the expert bundle lands when the
    operator unpacks one on the node, so a node can run Tor without touching the
    system PATH. An unpacked bundle keeps its version folder
    (``tor-expert-bundle-windows-x86_64-13.5/tor/tor.exe``), so one level of
    nesting is searched too — the file the operator dropped is the file we run.
    """
    found = shutil.which(str(settings.get("tor.binary") or "tor"))
    if found:
        return found
    bundled = data_dir("tor") / "runtime" / "tor"
    for name in ("tor.exe", "tor"):
        candidate = bundled / name
        if candidate.is_file():
            return str(candidate)
    if bundled.is_dir():
        for candidate in sorted(bundled.rglob("tor.exe" if os.name == "nt"
                                              else "tor")):
            if candidate.is_file():
                return str(candidate)
    return None


def control_reachable(timeout: float = 1.5) -> bool:
    """True when something answers on the control port (no authentication yet)."""
    try:
        with socket.create_connection((_host(), _control_port()),
                                      timeout=timeout):
            return True
    except OSError:
        return False


def torrc_path() -> Path:
    """The torrc this node runs Tor with (``tor.torrc``, else ``data/tor/torrc``)."""
    raw = str(settings.get("tor.torrc") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else data_dir("tor") / path
    return data_dir("tor") / "torrc"


def write_torrc() -> Path:
    """Write the torrc the bundled/system daemon is started with.

    SOCKS is what a replica needs to reach an .onion origin; the control port
    with cookie authentication is what ``ADD_ONION`` needs. Nothing else is
    enabled — this is a node, not an exit relay.
    """
    folder = data_dir("tor")
    (folder / "data").mkdir(parents=True, exist_ok=True)
    path = torrc_path()
    socks = int(settings.get("tor.socks_port") or 9050)
    lines = [
        "# written by oneness_tor.services.tor_service.write_torrc",
        f"SocksPort {socks}",
        f"ControlPort {_control_port()}",
        "CookieAuthentication 1",
        f"DataDirectory {(folder / 'data').as_posix()}",
        f"Log notice file {(folder / 'tor.log').as_posix()}",
        "ClientOnly 1",
        "AvoidDiskWrites 1",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def log_path() -> Path:
    return data_dir("tor") / "tor.log"


def ensure_daemon(*, wait: float | None = None, force: bool = False) -> dict:
    """Start ``tor`` when the control port is closed; report what really happened.

    Idempotent: an already-reachable control port is reported as ``running`` with
    ``started: False``, so the start command can call this on every boot. When the
    binary is missing the exception names the two fixes (install Tor, or drop the
    expert bundle in ``data/tor/runtime/tor``) instead of pretending an onion
    exists.
    """
    if not force and control_reachable():
        return {"running": True, "started": False, "binary": binary(),
                "control_port": _control_port(),
                "note": "the control port already answers — nothing was started"}
    executable = binary()
    if not executable:
        raise TorUnavailable(
            "tor is not installed on this host, so no onion can be published: "
            "install it (`apt-get install tor`, `brew install tor`, or the Tor "
            "expert bundle at data/tor/runtime/tor), or point at a control port "
            "with ONENESS_TOR_CONTROL_PORT / tor.hostname",
            details={"looked_for": str(settings.get("tor.binary") or "tor"),
                     "bundled": str(data_dir("tor") / "runtime" / "tor"),
                     "control_port": _control_port()})
    torrc = write_torrc()
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        handle = subprocess.Popen(  # noqa: S603 - the operator's own torrc
            [executable, "-f", str(torrc)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, cwd=str(data_dir("tor")),
            creationflags=creation, start_new_session=os.name != "nt")
    except OSError as problem:
        raise TorUnavailable(f"tor could not be started ({problem})",
                             details={"binary": executable,
                                      "torrc": str(torrc)}) from problem
    limit = float(wait if wait is not None else settings.get("tor.start_timeout")
                  or 25.0)
    deadline = time.time() + max(3.0, limit)
    while time.time() < deadline:
        if control_reachable():
            # the control port answers before the daemon has bootstrapped: give
            # it a moment so ADD_ONION right after this call is not a race
            time.sleep(0.4)
            return {"running": True, "started": True, "pid": handle.pid,
                    "binary": executable, "torrc": str(torrc),
                    "control_port": _control_port(),
                    "socks_port": int(settings.get("tor.socks_port") or 9050),
                    "log": str(log_path()),
                    "note": "tor started by this node (detached, cookie auth)"}
        if handle.poll() is not None:
            break
        time.sleep(0.25)
    tail = ""
    try:
        tail = log_path().read_text(encoding="utf-8", errors="replace")[-400:]
    except OSError:
        pass
    raise TorUnavailable(
        f"tor was started but control port {_host()}:{_control_port()} never "
        f"answered — open {torrc} and check its log",
        details={"pid": handle.pid, "torrc": str(torrc), "log": str(log_path()),
                 "tail": tail or None})


PACKAGE_MANAGERS = (
    # (executable, the commands that install `tor` with it)
    ("apt-get", (["apt-get", "update"],
                 ["apt-get", "install", "-y", "--no-install-recommends", "tor"])),
    ("apk", (["apk", "add", "--no-cache", "tor"],)),
    ("dnf", (["dnf", "install", "-y", "tor"],)),
    ("yum", (["yum", "install", "-y", "tor"],)),
    ("brew", (["brew", "install", "tor"],)),
)


def install_daemon(*, timeout: float = 900.0) -> dict:
    """Install the ``tor`` package with the host's own package manager.

    The image already ships ``tor`` (see the Dockerfile), so this exists for the
    hosts that do not: a Render service created from a *Python* runtime instead
    of this repository's Dockerfile, a bare VM, a fresh laptop. Nothing is faked —
    the exact commands, their exit codes and the tail of their output are
    returned, and a host with no known package manager gets the expert-bundle
    instructions instead of a silent no-op.
    """
    found = binary()
    if found:
        return {"installed": True, "already": True, "binary": found,
                "note": f"tor is already installed at {found} — nothing was run"}
    if os.name == "nt":
        raise TorUnavailable(
            "this host has no package manager this node can use: on Windows "
            "unpack the Tor expert bundle into data/tor/runtime/tor (the "
            "tor.exe inside it is run as-is) or install it and set "
            "ONENESS_TOR_BINARY to the tor.exe path",
            details={"looked_for": "tor", "bundled": str(data_dir("tor")
                                                         / "runtime" / "tor")})
    commands: tuple[list[str], ...] = ()
    manager = ""
    for executable, steps in PACKAGE_MANAGERS:
        if shutil.which(executable):
            manager, commands = executable, steps
            break
    if not commands:
        raise TorUnavailable(
            "no supported package manager (apt-get, apk, dnf, yum, brew) is on "
            "this host, so `tor` cannot be installed here: install it yourself "
            "and set ONENESS_TOR_BINARY, or point at a daemon with "
            "ONENESS_TOR_CONTROL_PORT / tor.hostname",
            details={"path": os.environ.get("PATH", "")[:400]})
    ran: list[dict] = []
    for command in commands:
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as problem:
            raise TorUnavailable(f"{' '.join(command)} could not run ({problem})",
                                 details={"command": " ".join(command)}) from problem
        output = ((done.stdout or "") + (done.stderr or "")).strip()
        ran.append({"command": " ".join(command), "exit": done.returncode,
                    "tail": output[-600:]})
        if done.returncode != 0:
            raise TorUnavailable(
                f"{' '.join(command)} exited {done.returncode}, so tor is still "
                f"missing: {output[-300:] or 'no output'}",
                details={"manager": manager, "commands": ran})
    found = binary()
    if not found:
        raise TorUnavailable(
            f"{manager} reported success but `tor` is still not on PATH: set "
            f"ONENESS_TOR_BINARY to the binary it installed",
            details={"manager": manager, "commands": ran})
    return {"installed": True, "already": False, "binary": found,
            "manager": manager, "commands": ran,
            "note": f"tor installed with {manager} at {found} — nothing was started"}


def _cookie_hex(protocol_lines: list[str]) -> str | None:
    """The control-auth cookie Tor printed in ``PROTOCOLINFO`` (hex), else ``None``."""
    for line in protocol_lines:
        if "COOKIEFILE=" in line:
            raw = line.split("COOKIEFILE=", 1)[1].strip().strip('"')
            try:
                with open(raw, "rb") as handle:
                    return handle.read().hex()
            except OSError:
                return None
    return None


class _Control:
    """Context manager around one authenticated control-port session."""

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = float(timeout)
        self.socket: socket.socket | None = None
        self.reader = None
        self.writer = None

    def __enter__(self) -> "_Control":
        try:
            self.socket = socket.create_connection((_host(), _control_port()),
                                                   timeout=self.timeout)
        except OSError as problem:
            installed = bool(binary())
            fix = ("set `tor.auto_start: true` (ONENESS_TOR_AUTOSTART=1) so this "
                   "node starts its own daemon, or start yours with `ControlPort "
                   f"{_control_port()}` + `CookieAuthentication 1`"
                   if installed else
                   "Tor is not installed: install it, or unpack the Tor expert "
                   "bundle into data/tor/runtime/tor")
            raise TorUnavailable(
                f"Tor control port {_host()}:{_control_port()} is not reachable "
                f"({problem}). {fix}.",
                details={"host": _host(), "port": _control_port(),
                         "installed": installed,
                         "auto_start": _auto_start()}) from problem
        self.reader = self.socket.makefile("rb")
        self.writer = self.socket.makefile("wb")
        code, lines = self.command("PROTOCOLINFO 1")
        cookie = _cookie_hex(lines)
        if cookie:
            self.command(f"AUTHENTICATE {cookie}")
        else:
            self.command("AUTHENTICATE")
        return self

    def __exit__(self, *exc_info) -> None:
        for handle in (self.reader, self.writer):
            try:
                handle.close()
            except Exception:
                pass
        try:
            self.socket.close()
        except Exception:
            pass

    def command(self, line: str) -> tuple[str, list[str]]:
        assert self.writer and self.reader
        self.writer.write(line.encode("utf-8") + b"\r\n")
        self.writer.flush()
        lines: list[str] = []
        while True:
            raw = self.reader.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            lines.append(text)
            if len(text) >= 4 and text[:3].isdigit() and text[3] == " ":
                break
            if len(text) == 3 and text.isdigit():
                break
        code = lines[0][:3] if lines else "000"
        if code.startswith(("4", "5")):
            raise TorUnavailable(f"Tor control said {lines[0]}",
                                 details={"command": line.split()[0],
                                          "reply": lines})
        return code, lines


def status() -> dict:
    """Install + control state + published services (never raises)."""
    installed = binary() is not None
    reachable, error = False, None
    try:
        with _Control(timeout=5) as control:
            code, lines = control.command("GETINFO version")
            reachable = code == "250"
            version = next((line.split("=", 1)[1] for line in lines
                            if line.startswith("250-version=")), None)
    except TorUnavailable as problem:
        error, version = problem.message, None
    services = [{"service_id": row.get("service_id"), "onion": row.get("onion"),
                 "site_id": row.get("site_id"), "path": row.get("path"),
                 "created_at": row.get("created_at")}
                for row in SERVICES.all()]
    node = node_status()
    return {"installed": installed, "binary": binary(), "reachable": reachable,
            "version": version, "control_port": _control_port(),
            "socks_port": int(settings.get("tor.socks_port") or 9050),
            "host": _host(), "services": services, "error": error,
            # what this node itself is published at, and whether it can start
            # its own daemon — the two facts an onion deployment is judged on
            "node": node, "onion": node.get("url"),
            "control_open": reachable or control_reachable(0.8),
            "auto_start": _auto_start(), "torrc": str(torrc_path()),
            "onion_key_set": bool(str(settings.get("tor.onion_key") or "").strip()),
            "hint": (None if reachable else
                     ("install it with `python -m oneness_tor.cli install` "
                      "(ONENESS_TOR_AUTO_INSTALL=1 installs at boot), or unpack "
                      "the Tor expert bundle into data/tor/runtime/tor")
                     if not installed else
                     ("send the daemon up with `python -m oneness_tor.cli start` "
                      "(auto_start is on)" if _auto_start() else
                      "start Tor with ControlPort + CookieAuthentication, or set "
                      "tor.auto_start: true so this node starts it")),
            "checked_at": int(time.time())}


def _auto_start() -> bool:
    value = settings.get("tor.auto_start")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def new_identity() -> dict:
    with _Control() as control:
        code, lines = control.command("SIGNAL NEWNYM")
    return {"signal": "NEWNYM", "reply": lines, "sent_at": int(time.time())}


def _key_blob(stored: object) -> str:
    """The bare base64 part of a recorded ``ED25519-V3:<blob>`` private key."""
    text = str(stored or "").strip()
    if ":" in text:
        prefix, _, rest = text.partition(":")
        if prefix.upper() == "ED25519-V3":
            return rest.strip()
    return text


def _key_bytes(key: object) -> bytes:
    """The 64 raw bytes of an ``ED25519-V3`` blob (``b""`` = not a usable key)."""
    blob = _key_blob(key)
    if not blob:
        return b""
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
    except (ValueError, TypeError):
        return b""
    return raw if len(raw) == 64 else b""


# The two constants of rend-spec-v3: the checksum prefix and the address version.
ONION_CHECKSUM_PREFIX = b".onion checksum"
ONION_VERSION = b"\x03"


def onion_address(key: object) -> str:
    """The ``<id>.onion`` an ``ED25519-V3`` key owns (``""`` when it is not one).

    Derived the way Tor derives it — ``base32(checksum | pubkey | 0x03)`` where
    the checksum is the first two bytes of
    ``SHA3-256(".onion checksum" | pubkey | 0x03)`` — so the address is known
    *before* a daemon exists. That is what lets a deploy log say which onion a
    hardcoded key owns even when Tor itself could not start, instead of leaving
    the operator with a key and no idea what it points at.
    """
    raw = _key_bytes(key)
    if not raw:
        return ""
    public = raw[32:]
    checksum = hashlib.sha3_256(ONION_CHECKSUM_PREFIX + public
                                + ONION_VERSION).digest()[:2]
    encoded = base64.b32encode(checksum + public + ONION_VERSION)
    return f"{encoded.decode('ascii').lower()}.onion"


def keygen(*, seed: bytes | None = None) -> dict:
    """A fresh ED25519-V3 onion key **and** the address it derives to.

    This is exactly what Tor mints on ``ADD_ONION NEW:ED25519-V3``: the private
    part is ``clamp(SHA512(seed)[0:32])`` followed by the ed25519 public key, 64
    bytes, base64. Generating it here means the address exists *before* the first
    deploy — paste the blob into ``ONENESS_TOR_ONION_KEY`` (or commit it for a
    fleet that publishes one identity) and every container re-creates the same
    hidden service without anybody shelling in.

    ``seed`` is only for tests: omit it and the key is from ``os.urandom``.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (Encoding,
                                                             PublicFormat)

    material = seed if seed is not None else os.urandom(32)
    if not isinstance(material, (bytes, bytearray)) or len(material) != 32:
        raise ValidationError("an ED25519 seed is exactly 32 bytes",
                              details={"bytes": len(bytes(material or b""))})
    expanded = bytearray(hashlib.sha512(bytes(material)).digest())
    expanded[0] &= 248                     # the standard ed25519 clamping
    expanded[31] &= 127
    expanded[31] |= 64
    private = Ed25519PrivateKey.from_private_bytes(bytes(material))
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    blob = base64.b64encode(bytes(expanded[:32]) + public).decode("ascii")
    address = onion_address(blob)
    return {"key": blob, "onion": address, "url": f"http://{address}",
            "env": "ONENESS_TOR_ONION_KEY", "seed_hex": bytes(material).hex()}


def _add_onion(target_port: int, virtual_port: int, key: object = None) -> str:
    """The control line that creates one hidden service.

    ``Flags=Detach`` is what makes the .onion outlive the control connection:
    without it Tor drops the service the moment this API's socket closes, so
    the address is recorded but never answers (Tor 0.4.9 does exactly that —
    "No more HSDir available to query"). A stored key re-creates the *same*
    address instead of minting a new one on every publish.
    """
    blob = _key_blob(key)
    spec = f"ED25519-V3:{blob}" if blob else "NEW:ED25519-V3"
    # The key blob comes first and the flags after it — ``ADD_ONION Flags=Detach
    # NEW:ED25519-V3 …`` is rejected with "Cannot parse keyword argument(s)".
    return (f"ADD_ONION {spec} Flags=Detach "
            f"Port={int(virtual_port)},127.0.0.1:{int(target_port)}")


def publish_path(path: str, target_port: int, *, virtual_port: int = 80,
                 site_id: str | None = None, build_id: str | None = None,
                 label: str = "hidden service", key: object = None) -> dict:
    """ADD_ONION for any served path; returns the real service id + .onion."""
    target = str(path or "/")
    if not target.startswith("/"):
        target = "/" + target
    with _Control() as control:
        code, lines = control.command(
            _add_onion(target_port, virtual_port, key))
    service_id = next((line.split("=", 1)[1] for line in lines
                       if "ServiceID=" in line), "")
    private_key = next((line.split("=", 1)[1] for line in lines
                        if "PrivateKey=" in line), "")
    if not private_key:
        # Tor omits PrivateKey when it already holds the service, so the key we
        # asked to reuse is the one to keep on record (otherwise the next
        # publish has nothing to re-create and the .onion would change).
        blob = _key_blob(key)
        private_key = f"ED25519-V3:{blob}" if blob else ""
    if not service_id:
        raise TorUnavailable(f"Tor did not return a ServiceID: {lines}",
                             details={"reply": lines})
    onion = f"{service_id}.onion"
    SERVICES.add({"site_id": site_id, "build_id": build_id, "label": label,
                  "service_id": service_id, "onion": onion,
                  "private_key": private_key, "target_port": int(target_port),
                  "virtual_port": int(virtual_port), "path": target,
                  "created_at": datetime.now(timezone.utc).isoformat(
                      timespec="seconds"), "created_unix": int(time.time())})
    return {"onion": onion, "service_id": service_id, "path": target,
            "port": int(target_port), "url": f"http://{onion}{target}"}


def publish_project(build_id: str, target_port: int, *,
                    virtual_port: int = 80) -> dict:
    """ADD_ONION for one DarkWeb builder project (``/builds/<id>/``)."""
    return publish_path(f"/builds/{build_id}/", target_port,
                        virtual_port=virtual_port, build_id=build_id,
                        label=f"build {build_id}")


def remove_service(service_id: str) -> dict:
    with _Control() as control:
        control.command(f"DEL_ONION {service_id}")
    return {"removed": service_id}


# ------------------------------------------------------------- the node itself
NODE_LABEL = "node"


def node_service() -> dict | None:
    """The recorded hidden service that publishes *this API* (if any)."""
    label = str(settings.get("tor.node_label") or NODE_LABEL)
    rows = [row for row in SERVICES.all() if str(row.get("label")) == label]
    return rows[-1] if rows else None


def node_key() -> str:
    """The private key of this node's onion: the recorded one, else ``tor.onion_key``.

    A Render container's disk is ephemeral, so the recorded key is gone after a
    redeploy. ``ONENESS_TOR_ONION_KEY`` carries the same ``ED25519-V3`` blob and
    makes ``ADD_ONION`` re-create the *same* address instead of minting a new one.
    """
    label = str(settings.get("tor.node_label") or NODE_LABEL)
    for row in reversed(SERVICES.all()):
        if str(row.get("label")) == label and row.get("private_key"):
            return str(row["private_key"])
    return str(settings.get("tor.onion_key") or "").strip()


def onion_key_env() -> str:
    """The bare base64 blob to paste into ``ONENESS_TOR_ONION_KEY`` (``""`` = none)."""
    return _key_blob(node_key())


def node_key_url() -> str:
    """``http://<id>.onion`` that the stored key alone proves this node owns.

    The key *is* the identity, so the address is knowable before — or without — a
    Tor daemon. The deploy log prints it, which turns ``tor is not installed``
    into "this node is meant to answer at <address>, its daemon did not start"
    instead of leaving the operator with a key and no address.
    """
    address = onion_address(node_key())
    return f"http://{address}" if address else ""


def node_url() -> str:
    """``http://<id>.onion`` for this node, or ``""`` when none was published."""
    onion = str((node_service() or {}).get("onion") or "")
    return f"http://{onion}" if onion else ""


def node_port() -> int:
    """The port the WSGI server actually bound (``--port`` wins over config)."""
    raw = os.environ.get("ONENESS_API_PORT") or settings.get("api.port") or 8090
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8090


def publish_node(target_port: object = None, *, refresh: bool = False) -> dict:
    """ADD_ONION for the whole API — the onion link this node is browsed at."""
    port = int(target_port or node_port())
    # A closed control port is the commonest surprise ("TOR_DAEMON_UNAVAILABLE …
    # control port 9051 is not reachable"): Tor is installed but nobody started
    # it. When this node owns the daemon (tor.auto_start) it starts it here —
    # the same call the start command makes, and idempotent; with no binary at
    # all ensure_daemon() raises the install hint instead.
    if not control_reachable(0.8) and _auto_start():
        ensure_daemon()
    existing = node_service()
    if (not refresh and existing and existing.get("onion")
            and int(existing.get("target_port") or 0) == port):
        return {"onion": existing["onion"],
                "service_id": existing.get("service_id"),
                "url": f"http://{existing['onion']}", "port": port,
                "reused": True,
                "note": ("recorded onion reused; Tor keeps a service only while "
                         "the daemon runs — send refresh:true to ADD_ONION again")}
    try:
        published = publish_path("/", port,
                                 label=str(settings.get("tor.node_label") or NODE_LABEL),
                                 key=node_key())
    except TorUnavailable as problem:
        if "collision" not in problem.message.lower() or not existing:
            raise
        # The daemon already has this key loaded, so the recorded address is
        # live — that is a success, not an error (DEL_ONION first to re-add).
        return {"onion": existing["onion"],
                "service_id": existing.get("service_id"),
                "url": f"http://{existing['onion']}", "port": port,
                "reused": True,
                "note": ("this onion is already loaded by the Tor daemon and "
                         "answering; DEL_ONION it first to publish a fresh "
                         "service")}
    # The key *is* the address: when Tor's ServiceID disagrees with the address the
    # stored key derives, the blob it read is not the blob we hold — say so instead
    # of silently publishing an onion that no redeploy will reproduce.
    derived = onion_address(node_key())
    if derived and derived != published.get("onion"):
        published["key_warning"] = (
            f"Tor published {published.get('onion')} for the configured key, but "
            f"that key derives {derived}: the value in ONENESS_TOR_ONION_KEY is "
            f"not the 64-byte ED25519-V3 blob this node holds (copy it exactly as "
            f"`torctl key` prints it — no 'ED25519-V3:' prefix, no quotes)")
    else:
        published["key_address"] = derived or published.get("onion")
    return {**published, "reused": False,
            "note": "ADD_ONION accepted by the Tor control port"}


def node_status() -> dict:
    row = node_service() or {}
    url = node_url()
    return {"published": bool(row.get("onion")), "onion": row.get("onion"),
            "url": url or None, "service_id": row.get("service_id"),
            "target_port": row.get("target_port"), "port": node_port(),
            "created_at": row.get("created_at"),
            "key_set": bool(onion_key_env()),
            "key_address": onion_address(node_key()) or None,
            "key_env": "ONENESS_TOR_ONION_KEY",
            "auto_start": _auto_start(), "torrc": str(torrc_path()),
            "paths": {"docs": f"{url}/docs" if url else None,
                      "tokens": f"{url}/api/tokens/public" if url else None},
            "note": ("an onion link only works after ADD_ONION; Tor must be "
                     "installed with ControlPort + CookieAuthentication enabled "
                     "(tor.auto_start lets this node start it). Copy the key into "
                     "ONENESS_TOR_ONION_KEY so a redeploy keeps the same address")}


def unpublish_node() -> dict:
    """DEL_ONION the service that publishes this API (the address stops answering)."""
    row = node_service()
    if not row or not row.get("service_id"):
        raise TorUnavailable(
            "this node has no recorded onion to remove — nothing was published",
            details={"label": str(settings.get("tor.node_label") or NODE_LABEL)})
    removed = remove_service(str(row["service_id"]))
    return {**removed, "onion": row.get("onion"),
            "note": ("DEL_ONION accepted; the private key stays on record, so the "
                     "next publish re-creates the same address")}

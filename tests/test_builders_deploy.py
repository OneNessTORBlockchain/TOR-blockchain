"""Both builders deploy for real: DarkWeb → onion URL, Rust → owner-only program.

The Tor test speaks the real control protocol (PROTOCOLINFO / AUTHENTICATE /
ADD_ONION) over a socket, so ``tor_service`` connects, authenticates and gets a
service id exactly as it would from a running Tor daemon. The Rust test runs a
real ``cargo build --release``.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import threading
from pathlib import Path

import pytest

from core.errors import Forbidden, TorUnavailable
from oneness_tor.services import contracts, tor_service, wallet_access, workbench


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


class FakeTor(threading.Thread):
    """A minimal control port: PROTOCOLINFO, AUTHENTICATE, ADD_ONION, DEL_ONION."""

    daemon = True

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []
        self.count = 0
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._closed = False

    def run(self) -> None:
        while not self._closed:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,),
                             daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        handle = connection.makefile("rwb")
        try:
            while True:
                raw = handle.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    return
                self.commands.append(line)
                command = line.split()[0].upper()
                if command == "PROTOCOLINFO":
                    reply = "250-PROTOCOLINFO 1\r\n250-AUTH METHODS=NULL\r\n250 OK\r\n"
                elif command == "ADD_ONION":
                    self.count += 1
                    service = f"fakeonion{self.count:02d}" + "b" * 40
                    reply = (f"250-ServiceID={service}\r\n"
                             "250-PrivateKey=ED25519-V3:fake\r\n250 OK\r\n")
                else:
                    reply = "250 OK\r\n"
                handle.write(reply.encode("utf-8"))
                handle.flush()
        except OSError:
            return
        finally:
            try:
                handle.close()
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass


@pytest.fixture
def fake_tor(monkeypatch):
    server = FakeTor()
    server.start()
    monkeypatch.setenv("ONENESS_TOR_CONTROL_PORT", str(server.port))
    yield server
    server.close()


def test_darkweb_builder_publishes_a_real_onion_url(sandbox, fake_tor):
    project = workbench.create("neon onion shop", "web")
    result = workbench.deploy(project["id"], wallet="founder")
    deployment = result["deployment"]

    assert deployment["kind"] == "web"
    assert deployment["onion"].endswith(".onion")
    assert deployment["onion_url"] == (
        f"http://{deployment['onion']}/builds/{project['id']}/")
    assert any(step.startswith("ADD_ONION") for step in fake_tor.commands)

    recorded = next(row for row in tor_service.SERVICES.all()
                    if row.get("build_id") == project["id"])
    assert recorded["path"] == f"/builds/{project['id']}/"
    assert recorded["onion"] == deployment["onion"]

    stored = workbench.get_project(project["id"], with_tree=False)
    assert stored["deployment"]["onion_url"] == deployment["onion_url"]

    served = client().get(f"/builds/{project['id']}/")
    assert served.status_code == 200
    assert b"<html" in served.data.lower()


def test_tor_failure_is_reported_honestly(sandbox, monkeypatch):
    monkeypatch.setenv("ONENESS_TOR_CONTROL_PORT", "1")  # nothing listens there
    project = workbench.create("unreachable", "web")
    with pytest.raises(Exception) as problem:
        workbench.deploy(project["id"])
    assert getattr(problem.value, "code", "") == "TOR_DAEMON_UNAVAILABLE"


def test_a_bundled_tor_runtime_is_found_without_touching_the_path(sandbox, monkeypatch):
    """The expert bundle lives in ``data/tor/runtime/tor`` on an operator node."""
    monkeypatch.setattr(tor_service.shutil, "which", lambda name: None)
    bundled = (sandbox / "tor" / "runtime" / "tor"
               / ("tor.exe" if os.name == "nt" else "tor"))
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    assert tor_service.binary() == str(bundled)


def test_an_unpacked_expert_bundle_is_found_inside_its_version_folder(sandbox,
                                                                     monkeypatch):
    """Windows unzips to ``tor-expert-bundle-.../tor/tor.exe``: still found."""
    monkeypatch.setattr(tor_service.shutil, "which", lambda name: None)
    binary = (sandbox / "tor" / "runtime" / "tor"
              / "tor-expert-bundle-windows-x86_64-13.5" / "tor"
              / ("tor.exe" if os.name == "nt" else "tor"))
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    assert tor_service.binary() == str(binary)
    assert tor_service.status()["installed"] is True


def test_publish_starts_a_daemon_it_owns_instead_of_giving_up(sandbox, monkeypatch):
    """A closed control port with ``tor.auto_start`` on: start it, do not fail."""
    monkeypatch.setenv("ONENESS_TOR_AUTOSTART", "1")
    monkeypatch.setattr(tor_service, "control_reachable", lambda *a, **k: False)
    started: list = []

    def fake_ensure_daemon(**kwargs):
        started.append(kwargs)
        return {"running": True, "started": True, "control_port": 9051}

    class Quiet:
        def command(self, line):
            return "250", ["250-ServiceID=" + "q" * 56]

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tor_service, "ensure_daemon", fake_ensure_daemon)
    monkeypatch.setattr(tor_service, "_Control", lambda *a, **k: Quiet())
    published = tor_service.publish_node(8090)
    assert published["onion"] == "q" * 56 + ".onion"
    assert started, "the node must start the daemon it owns"


def test_a_key_tor_already_holds_is_still_recorded(sandbox, monkeypatch):
    """Tor returns no PrivateKey when it already has the service; the record
    must keep the key anyway, or the next publish mints a new .onion."""
    service = "k" * 56

    class Quiet:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def command(self, line):
            return "250", [f"250-ServiceID={service}"]

    monkeypatch.setattr(tor_service, "_Control", lambda *args, **kwargs: Quiet())
    published = tor_service.publish_path("/", 8090, key="ED25519-V3:abc123")
    stored = tor_service.SERVICES.all()[-1]
    assert published["onion"] == f"{service}.onion"
    assert stored["private_key"] == "ED25519-V3:abc123"


def test_a_live_service_key_comes_back_as_reused_not_as_an_error(sandbox, monkeypatch):
    """A second ADD_ONION of the same key is ``550 Onion address collision`` —
    the address is already answering, which is the outcome the operator wants."""
    service = "m" * 56
    tor_service.SERVICES.add({"label": "node", "service_id": service,
                              "onion": f"{service}.onion", "target_port": 8090,
                              "virtual_port": 80, "path": "/",
                              "private_key": "ED25519-V3:abc123"})

    class Collides:
        def command(self, line):
            raise TorUnavailable("Tor control said 550 Onion address collision",
                                 details={"command": "ADD_ONION"})

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tor_service, "_Control", lambda *args, **kwargs: Collides())
    published = tor_service.publish_node(8090, refresh=True)
    assert published["reused"] is True
    assert published["onion"] == f"{service}.onion"


def test_hidden_services_are_detached_so_the_onion_outlives_the_socket(sandbox, fake_tor):
    """Tor drops an ADD_ONION service when the control connection closes —
    unless the command carries ``Flags=Detach`` (observed on Tor 0.4.9: the
    address was recorded and nothing ever answered)."""
    published = tor_service.publish_path("/", 8090, label="node")
    assert published["onion"].endswith(".onion")
    command = next(step for step in fake_tor.commands if step.startswith("ADD_ONION"))
    words = command.split()
    assert words[1] == "NEW:ED25519-V3"      # key blob first, then the flags
    assert "Flags=Detach" in words
    assert words[words.index("Flags=Detach") - 1] == "NEW:ED25519-V3"


def test_a_recorded_key_recreates_the_same_address_instead_of_a_new_one(sandbox, fake_tor):
    tor_service.publish_node(8090)
    stored = tor_service.node_service()
    assert stored["private_key"].startswith("ED25519-V3:")
    fake_tor.commands.clear()
    tor_service.publish_node(8090, refresh=True)
    again = next(step for step in fake_tor.commands if step.startswith("ADD_ONION"))
    assert again.split()[1] == f"ED25519-V3:{stored['private_key'].split(':', 1)[1]}"
    assert "Flags=Detach" in again.split()


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_builder_compiles_and_registers_an_owner_only_program(sandbox, make_wallet):
    owner = make_wallet("u_owner")
    stranger = make_wallet("u_stranger")
    project = workbench.create("owner program", "rust")

    result = workbench.deploy(project["id"], wallet="u_owner")
    program = result["program"]
    deployment = result["deployment"]

    assert program["kind"] == "rust_program"
    assert program["owner_address"] == owner["address"]
    binary = Path(deployment["binary"])
    assert binary.is_file()
    digest = hashlib.sha3_256(binary.read_bytes()).hexdigest()
    assert deployment["binary_sha3"] == digest == program["binary_sha3"]

    assert contracts.get_contract(program["id"])["access"] == "summary"
    full = contracts.get_contract(program["id"], viewer_address=owner["address"])
    assert full["source"].strip() and full["binary_path"] == str(binary)

    with pytest.raises(Forbidden):
        contracts.call(program["id"], "status", actor_address=stranger["address"])
    managed = contracts.call(program["id"], "status", actor_address=owner["address"])
    assert managed["state"]["last_call"]["function"] == "status"

    http = client()
    session = wallet_access.open_session("u_owner", owner["address"])
    seen = http.get(f"/api/contracts/{program['id']}",
                    headers={"x-wallet-session": session["session"]})
    assert seen.get_json()["data"]["contract"]["access"] == "owner"

    stored = workbench.get_project(project["id"], with_tree=False)
    assert stored["owner_address"] == owner["address"]
    assert stored["deployment"]["program_id"] == program["id"]

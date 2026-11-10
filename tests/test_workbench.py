"""Offline tests: the app builders, per-surface model routing and the sync audit.

No Ollama and no chain are required — generation and the node are monkeypatched,
and the honest failure paths are asserted instead of being papered over.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from core import ollama
from core.errors import ChainUnavailable, NotFound, ValidationError
from oneness_tor.services import node_sync, workbench


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolated data dir, so projects land in tmp and never in the repo."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ONENESS_DATA_DIR", str(data))
    # Offline: no /api/show probe. Tests that care set this themselves.
    monkeypatch.setattr(ollama, "supports_thinking", lambda model: False)
    return data


# ------------------------------------------------------------------ projects
def test_create_scaffolds_real_files(sandbox):
    project = workbench.create("Neon Shop", "web")
    assert project["kind"] == "web"
    assert [row["path"] for row in project["tree"]] == [
        "README.md", "app.js", "index.html", "styles.css"]
    folder = workbench.project_dir(project["id"])
    html = (folder / "index.html").read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>") and "Neon Shop" in html
    assert project["preview_url"].endswith(f"/builds/{project['id']}/")


def test_create_scaffolds_a_cargo_project(sandbox):
    project = workbench.create("Chain Tool", "rust")
    paths = [row["path"] for row in project["tree"]]
    assert "Cargo.toml" in paths and "src/main.rs" in paths
    folder = workbench.project_dir(project["id"])
    cargo = (folder / "Cargo.toml").read_text(encoding="utf-8")
    main = (folder / "src/main.rs").read_text(encoding="utf-8")
    assert 'name = "chain-tool"' in cargo
    assert "TcpListener::bind" in main and "PORT" in main


def test_create_rejects_an_unknown_kind_and_an_empty_name(sandbox):
    with pytest.raises(ValidationError):
        workbench.create("Thing", "java")
    with pytest.raises(ValidationError):
        workbench.create("   ", "web")


def test_file_round_trip_create_read_append_delete(sandbox):
    project = workbench.create("Editor Shop", "web")
    written = workbench.write_file(project["id"], "notes/todo.md", "one\n")
    assert written["created"] is True and written["language"] == "markdown"
    workbench.write_file(project["id"], "notes/todo.md", "two\n", mode="append")
    read = workbench.read_file(project["id"], "notes/todo.md")
    assert read["content"] == "one\ntwo\n"
    with pytest.raises(ValidationError):
        workbench.write_file(project["id"], "notes/todo.md", "x", mode="create")
    removed = workbench.delete_file(project["id"], "notes/todo.md")
    assert removed["deleted"] == "notes/todo.md"
    assert "notes/todo.md" not in [row["path"] for row in workbench.tree(project["id"])]
    with pytest.raises(NotFound):
        workbench.read_file(project["id"], "notes/todo.md")


def test_paths_can_never_escape_the_project(sandbox):
    project = workbench.create("Safe Shop", "web")
    for bad in ("../projects.json", "/etc/passwd", "C:/Windows/win.ini",
                "a/../../b.txt", "~/secret"):
        with pytest.raises(ValidationError):
            workbench.read_file(project["id"], bad)
    with pytest.raises(NotFound):
        workbench.read_file(project["id"], "nope.txt")


def test_write_requires_text(sandbox):
    project = workbench.create("Binary Shop", "web")
    with pytest.raises(ValidationError):
        workbench.write_file(project["id"], "blob.bin", b"\x00\x01")


def test_unknown_project_is_a_404(sandbox):
    with pytest.raises(NotFound):
        workbench.get_project("build_9999")


# ----------------------------------------------------------------- run/install
def test_run_executes_in_the_project_directory(sandbox):
    project = workbench.create("Runner", "web")
    workbench.write_file(project["id"], "hello.py", "print('from the project')\n")
    result = workbench.run_command(project["id"], f'python hello.py', timeout=60)
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "from the project"
    assert workbench.get_project(project["id"])["last_run"]["ok"] is True


def test_run_refuses_shell_operators_and_unknown_programs(sandbox):
    project = workbench.create("Guarded", "web")
    with pytest.raises(ValidationError) as problem:
        workbench.run_command(project["id"], "python -c \"print(1)\" && echo done")
    assert "not a shell" in problem.value.message
    with pytest.raises(ValidationError) as problem:
        workbench.run_command(project["id"], "powershell -Command Get-Process")
    assert "allow-list" in problem.value.message


def test_run_reports_a_missing_tool_honestly(sandbox, monkeypatch):
    project = workbench.create("Missing Tool", "web")
    monkeypatch.setattr(workbench, "_which", lambda program: None)
    with pytest.raises(ValidationError) as problem:
        workbench.run_command(project["id"], "cargo build")
    assert "not installed" in problem.value.message


def test_install_builds_the_right_argv(sandbox):
    assert workbench._cargo_argv(["serde"]) == ["cargo", "add", "serde"]
    assert workbench._cargo_argv([]) == ["cargo", "fetch"]
    assert workbench._npm_argv(["express"])[:3] == ["npm", "install", "--no-audit"]
    project = workbench.create("Installer", "web")
    with pytest.raises(ValidationError):
        workbench.install(project["id"], "apt", "curl")
    with pytest.raises(ValidationError):
        workbench.install(project["id"], "npm", "")


def test_install_cargo_without_packages_fetches(sandbox, monkeypatch):
    project = workbench.create("Fetcher", "rust")
    seen: dict = {}

    def fake_run(build_id, command, **kwargs):
        seen["argv"] = kwargs.get("argv")
        seen["timeout"] = kwargs.get("timeout")
        return {"command": command, "argv": kwargs.get("argv"), "exit_code": 0,
                "ok": True, "stdout": "", "stderr": "", "duration_ms": 1,
                "timed_out": False}

    monkeypatch.setattr(workbench, "_run", fake_run)
    result = workbench.install(project["id"], "cargo", "")
    assert seen["argv"] == ["cargo", "fetch"]
    assert result["packages"] == [] and result["manager"] == "cargo"


# ------------------------------------------------------------------- AI edits
def test_ai_edit_applies_a_json_file_plan(sandbox, monkeypatch):
    project = workbench.create("AI Shop", "web")
    plan = {"answer": "added a hero",
            "files": [{"path": "index.html", "content": "<!DOCTYPE html><html><body>new</body></html>"},
                      {"path": "js/hero.js", "content": "export const hero = 1;\n"}],
            "delete": ["styles.css"]}
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": json.dumps(plan), "model": args[0]})
    result = workbench.ai_edit(project["id"], "add a hero")
    assert result["purpose"] == "web_builder"
    assert {row["path"] for row in result["applied"]} == {"index.html", "js/hero.js"}
    assert result["removed"] == ["styles.css"]
    assert workbench.read_file(project["id"], "index.html")["content"].endswith("new</body></html>")
    assert "styles.css" not in [row["path"] for row in workbench.tree(project["id"])]
    assert workbench.get_project(project["id"])["edits"][0]["prompt"] == "add a hero"


def test_ai_edit_uses_the_rust_surface(sandbox, monkeypatch):
    project = workbench.create("AI Rust", "rust")
    seen = {}

    def fake_generate(model, prompt, **kwargs):
        seen["model"] = model
        seen["system"] = kwargs.get("system", "")
        return {"response": '{"answer":"ok","files":[{"path":"src/main.rs","content":"fn main() {}\\n"}]}',
                "model": model}

    monkeypatch.setattr(ollama, "generate", fake_generate)
    result = workbench.ai_edit(project["id"], "print hello")
    assert result["purpose"] == "rust_builder"
    assert "Rust Program Builder" in seen["system"]
    assert result["applied"][0]["path"] == "src/main.rs"


def test_ai_edit_falls_back_to_fenced_code(sandbox, monkeypatch):
    project = workbench.create("Fenced Shop", "web")
    body = "Here you go:\n```html\n<!DOCTYPE html><html><body>fenced</body></html>\n```"
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": body, "model": args[0]})
    result = workbench.ai_edit(project["id"], "make it fenced")
    assert result["applied"][0]["path"] == "index.html"
    assert "fenced" in workbench.read_file(project["id"], "index.html")["content"]


def test_ai_edit_refuses_prose(sandbox, monkeypatch):
    project = workbench.create("Prose Shop", "web")
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": "Sure, I would add a hero.",
                                                 "model": args[0]})
    with pytest.raises(ValidationError) as problem:
        workbench.ai_edit(project["id"], "add a hero")
    assert "JSON file plan" in problem.value.message


def test_ai_edit_switches_reasoning_off_for_a_thinking_model(sandbox, monkeypatch):
    """deepseek / glm / gpt-oss can spend the whole budget in ``thinking``."""
    project = workbench.create("Thinking Shop", "web")
    seen: dict = {}

    def fake_generate(model, prompt, **kwargs):
        seen.update(kwargs)
        return {"response": json.dumps({"answer": "ok", "files": [
            {"path": "index.html", "content": "<html>thought off</html>"}]}),
            "model": model, "done_reason": "stop"}

    monkeypatch.setattr(ollama, "supports_thinking", lambda model: True)
    monkeypatch.setattr(ollama, "generate", fake_generate)
    result = workbench.ai_edit(project["id"], "build it", model="deepseek:cloud")
    assert seen["think"] is False
    assert seen["format"] == "json"
    assert result["applied"][0]["path"] == "index.html"


def test_ai_edit_never_sends_think_to_a_plain_model(sandbox, monkeypatch):
    project = workbench.create("Plain Shop", "web")
    seen: dict = {}

    def fake_generate(model, prompt, **kwargs):
        seen.update(kwargs)
        return {"response": json.dumps({"answer": "ok", "files": [
            {"path": "index.html", "content": "<html>plain</html>"}]}),
            "model": model, "done_reason": "stop"}

    monkeypatch.setattr(ollama, "generate", fake_generate)
    workbench.ai_edit(project["id"], "build it", model="gemma3:1b")
    assert seen["think"] is None


def test_ai_edit_retries_a_reply_cut_off_at_the_token_limit(sandbox, monkeypatch):
    project = workbench.create("Long Shop", "web")
    calls: list[int] = []
    whole = json.dumps({"answer": "done", "files": [
        {"path": "index.html", "content": "<!DOCTYPE html><html>whole</html>"}]})

    def fake_generate(model, prompt, **kwargs):
        calls.append(kwargs["options"]["num_predict"])
        if len(calls) == 1:
            return {"response": '{"answer": "", "files": [{"path": "index.html", "cont',
                    "model": model, "done_reason": "length"}
        return {"response": whole, "model": model, "done_reason": "stop"}

    monkeypatch.setattr(ollama, "generate", fake_generate)
    result = workbench.ai_edit(project["id"], "write the page", model="fake:1b")
    assert calls == [4096, 8192]
    assert result["applied"][0]["path"] == "index.html"
    assert result["warnings"] == []


def test_ai_edit_salvages_complete_files_from_a_cut_off_reply(sandbox, monkeypatch):
    project = workbench.create("Salvage Shop", "web")
    cut = ('{"answer": "two files", "files": ['
           '{"path": "index.html", "content": "<!DOCTYPE html><html>one</html>"},'
           '{"path": "app.js", "content": "export const two = ')
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": cut, "model": args[0],
                                                 "done_reason": "length"})
    result = workbench.ai_edit(project["id"], "add both files", model="fake:1b")
    assert [row["path"] for row in result["applied"]] == ["index.html"]
    assert any("token limit" in message for message in result["warnings"])
    assert "one" in workbench.read_file(project["id"], "index.html")["content"]


def test_ai_edit_names_the_real_cause_when_a_model_only_thinks(sandbox, monkeypatch):
    project = workbench.create("Quiet Thinker", "web")
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": "", "thinking": "x" * 900,
                                                 "model": args[0],
                                                 "done_reason": "length"})
    with pytest.raises(ValidationError) as problem:
        workbench.ai_edit(project["id"], "add a hero", model="deepseek:cloud")
    assert "thinking" in problem.value.message
    assert problem.value.details["thinking_chars"] == 900


def test_ai_edit_never_deletes_the_entry_point(sandbox, monkeypatch):
    project = workbench.create("Guarded Shop", "web")
    plan = {"answer": "cleanup", "files": [], "delete": ["index.html", "styles.css"]}
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": json.dumps(plan),
                                                 "model": args[0]})
    result = workbench.ai_edit(project["id"], "remove the stylesheet")
    assert result["removed"] == ["styles.css"]
    assert any("entry point" in message for message in result["warnings"])
    assert workbench.read_file(project["id"], "index.html")["bytes"] > 0


def test_ai_edit_rejects_an_absolute_path_instead_of_writing_outside(sandbox, monkeypatch):
    project = workbench.create("Absolute Shop", "web")
    plan = {"answer": "x", "files": [{"path": "/workbench/index.html",
                                      "content": "<html></html>"}]}
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": json.dumps(plan),
                                                 "model": args[0]})
    with pytest.raises(ValidationError):
        workbench.ai_edit(project["id"], "write it")


def test_ai_edit_dry_run_reports_the_plan_without_writing(sandbox, monkeypatch):
    project = workbench.create("Dry Shop", "web")
    plan = {"answer": "would rewrite the page",
            "files": [{"path": "index.html", "content": "<html>dry</html>"}]}
    monkeypatch.setattr(ollama, "generate",
                        lambda *args, **kwargs: {"response": json.dumps(plan),
                                                 "model": args[0]})
    result = workbench.ai_edit(project["id"], "rewrite", apply=False)
    assert result["applied"] == [] and result["edit"]["files"] == ["index.html"]
    assert "dry" not in workbench.read_file(project["id"], "index.html")["content"]


def test_ai_edit_requires_a_prompt(sandbox):
    project = workbench.create("Quiet Shop", "web")
    with pytest.raises(ValidationError):
        workbench.ai_edit(project["id"], "  ")



# ------------------------------------------------------------ model routing
class RoutingSettings:
    """A settings stub: ``get`` by dotted path plus the data dir."""

    def __init__(self, raw: dict, data_dir):
        self.raw = raw
        self.data_dir = data_dir

    def get(self, path: str, default=None):
        node = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


@pytest.fixture()
def routed(sandbox, monkeypatch):
    settings_stub = RoutingSettings({
        "ollama": {"model": "base:1b",
                   "models": {"nft": "nft:2b", "web_builder": ""}}}, sandbox)
    monkeypatch.setattr(ollama, "settings", settings_stub)
    monkeypatch.setattr(ollama, "models", lambda: [
        {"name": "base:1b"}, {"name": "nft:2b"}, {"name": "llama3.2:3b"}])
    return settings_stub


def test_each_surface_resolves_its_own_model(routed):
    report = ollama.routing()
    by_id = {row["id"]: row for row in report["purposes"]}
    assert set(by_id) == {"brain", "nft", "web_builder", "rust_builder"}
    assert by_id["brain"]["model"] == "base:1b"
    assert by_id["brain"]["source"] == "ollama.model"
    assert by_id["nft"]["model"] == "nft:2b"
    assert by_id["nft"]["source"] == "config.yaml"
    assert by_id["rust_builder"]["model"] == "base:1b"
    assert all(row["installed"] for row in report["purposes"])
    assert report["default_model"] == "base:1b"


def test_set_routing_writes_an_override_for_one_surface(routed, sandbox):
    saved = ollama.set_routing("rust_builder", "llama3.2:3b")
    assert saved["model"] == "llama3.2:3b" and saved["cleared"] is False
    assert ollama.configured_model("rust_builder") == "llama3.2:3b"
    assert ollama.configured_model("brain") == "base:1b"
    assert ollama.model_for("rust_builder") == "llama3.2:3b"
    assert (sandbox / "ollama_routing.json").exists()
    cleared = ollama.set_routing("rust_builder", "")
    assert cleared["cleared"] is True
    assert ollama.configured_model("rust_builder") == "base:1b"


def test_set_routing_only_accepts_installed_models(routed):
    with pytest.raises(ValidationError) as problem:
        ollama.set_routing("brain", "ghost:70b")
    assert "not installed" in problem.value.message
    assert "llama3.2:3b" in problem.value.details["installed"]
    with pytest.raises(ValidationError):
        ollama.set_routing("nope", "base:1b")


def test_set_routing_refuses_to_guess_when_ollama_is_down(routed, monkeypatch):
    from core.errors import OllamaUnavailable

    def down():
        raise OllamaUnavailable("Ollama is not answering at http://127.0.0.1:11434")

    monkeypatch.setattr(ollama, "models", down)
    with pytest.raises(ValidationError) as problem:
        ollama.set_routing("brain", "llama3.2:3b")
    assert "cannot verify" in problem.value.message


def test_pick_model_uses_the_surface_override(routed):
    ollama.set_routing("nft", "llama3.2:3b")
    rows = [{"name": "base:1b"}, {"name": "llama3.2:3b"}]
    assert ollama.pick_model(model_rows=rows, purpose="nft") == "llama3.2:3b"
    assert ollama.pick_model(model_rows=rows, purpose="brain") == "base:1b"
    assert ollama.pick_model("explicit:1b", model_rows=rows, purpose="nft") == "explicit:1b"



# ------------------------------------------------------------- sync audit
def _block(height: int, previous: str, timestamp: int = 1_700_000_000) -> dict:
    block = {"height": height, "prev_hash": previous, "timestamp": timestamp,
             "proposer": "TOR1proposer", "state_root": "ab" * 32,
             "txids": [f"tx-{height}"]}
    block["hash"] = node_sync.block_digest(block)
    return block


def test_block_digest_matches_the_ledgers_canonical_payload():
    block = {"height": 3, "prev_hash": "cd" * 32, "timestamp": 1_700_000_123,
             "proposer": "TOR1proposer", "state_root": "ef" * 32,
             "txids": ["tx-a", "tx-b"]}
    expected = hashlib.sha3_256(json.dumps({
        "height": 3, "prev_hash": "cd" * 32, "proposer": "TOR1proposer",
        "state_root": "ef" * 32, "timestamp": 1_700_000_123,
        "txids": ["tx-a", "tx-b"]},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert node_sync.block_digest(block) == expected


def test_audit_reports_node_down(sandbox, monkeypatch):
    def down():
        raise ChainUnavailable("the TOR node at http://127.0.0.1:9 is not answering")

    monkeypatch.setattr(node_sync.node, "info", down)
    report = node_sync.audit()
    assert report["verdict"] == "node-down"
    assert report["node_reachable"] is False and report["errors"]
    assert report["blocks_checked"] == 0


def test_audit_rehashes_every_block(sandbox, monkeypatch):
    blocks = [_block(0, "0" * 64)]
    blocks.append(_block(1, blocks[0]["hash"]))
    info = {"height": 1, "head_hash": blocks[-1]["hash"],
            "genesis_hash": blocks[0]["hash"]}
    monkeypatch.setattr(node_sync.node, "info", lambda: {"ok": True, "data": info})
    monkeypatch.setattr(node_sync.node, "blocks", lambda start, end=None: {
        "ok": True, "data": {"blocks": [
            row for row in blocks
            if row["height"] >= start and (end is None or row["height"] <= end)]}})
    report = node_sync.audit()
    assert report["verdict"] == "in-sync", report["errors"]
    assert report["blocks_checked"] == 2
    assert report["truncated"] is False


def test_audit_detects_a_tampered_block(sandbox, monkeypatch):
    blocks = [_block(0, "0" * 64)]
    blocks.append(_block(1, blocks[0]["hash"]))
    blocks[1]["timestamp"] += 1  # the stored hash no longer matches the payload
    info = {"height": 1, "head_hash": blocks[-1]["hash"],
            "genesis_hash": blocks[0]["hash"]}
    monkeypatch.setattr(node_sync.node, "info", lambda: {"ok": True, "data": info})
    monkeypatch.setattr(node_sync.node, "blocks", lambda start, end=None: {
        "ok": True, "data": {"blocks": blocks}})
    report = node_sync.audit()
    assert report["verdict"] == "drift"
    assert any("does not re-hash" in message for message in report["errors"])



# -------------------------------------------------------------------- routes
def test_workbench_routes_serve_real_files(sandbox, monkeypatch):
    from oneness_tor.app import create_app
    from oneness_tor.services import auth

    def down():
        raise ChainUnavailable("the TOR node is not answering")

    monkeypatch.setattr(auth, "is_admin", lambda token: str(token or "") == "test-token")
    monkeypatch.setattr(node_sync.node, "info", down)
    monkeypatch.setattr(ollama, "models", lambda: [{"name": "llama3.2:3b"}])
    client = create_app().test_client()

    project = workbench.create("Route Shop", "web")
    listing = client.get("/api/workbench/projects").get_json()
    assert listing["ok"] is True and listing["data"]["count"] == 1
    detail = client.get(f"/api/workbench/projects/{project['id']}").get_json()
    assert detail["data"]["project"]["entry"] == "index.html"

    assert client.post("/api/workbench/projects", json={"name": "nope"}).status_code == 403
    created = client.post("/api/workbench/projects",
                          json={"name": "Route Rust", "kind": "rust"},
                          headers={"x-admin-token": "test-token"})
    assert created.status_code == 200
    assert created.get_json()["data"]["project"]["kind"] == "rust"

    read = client.get(f"/api/workbench/projects/{project['id']}/files?path=index.html")
    assert read.get_json()["data"]["language"] == "html"
    saved = client.post(f"/api/workbench/projects/{project['id']}/files",
                        json={"path": "index.html",
                              "content": "<!DOCTYPE html><html></html>"},
                        headers={"x-admin-token": "test-token"})
    assert saved.status_code == 200 and saved.get_json()["data"]["created"] is False

    preview = client.get(f"/builds/{project['id']}/")
    assert preview.status_code == 200 and b"<!DOCTYPE html>" in preview.data
    assert client.get(f"/builds/{project['id']}/nope.html").status_code == 404
    assert client.get("/builds/build_9999/").status_code == 404


def test_routing_and_sync_audit_routes(sandbox, monkeypatch):
    from oneness_tor.app import create_app
    from oneness_tor.services import auth

    def down():
        raise ChainUnavailable("the TOR node is not answering")

    monkeypatch.setattr(auth, "is_admin", lambda token: str(token or "") == "test-token")
    monkeypatch.setattr(node_sync.node, "info", down)
    monkeypatch.setattr(ollama, "models", lambda: [{"name": "llama3.2:3b"}])
    client = create_app().test_client()

    routing = client.get("/api/ollama/routing").get_json()
    assert routing["ok"] is True
    assert {row["id"] for row in routing["data"]["purposes"]} == {
        "brain", "nft", "web_builder", "rust_builder"}
    assert routing["data"]["models"][0]["name"] == "llama3.2:3b"

    saved = client.post("/api/ollama/routing",
                        json={"purpose": "nft", "model": "llama3.2:3b"},
                        headers={"x-admin-token": "test-token"})
    assert saved.status_code == 200
    assert saved.get_json()["data"]["model"] == "llama3.2:3b"

    rejected = client.post("/api/ollama/routing",
                           json={"purpose": "nft", "model": "ghost:70b"},
                           headers={"x-admin-token": "test-token"})
    assert rejected.status_code == 400
    assert "not installed" in rejected.get_json()["error"]["message"]

    audit = client.get("/api/chain/sync/audit").get_json()
    assert audit["ok"] is True and audit["data"]["verdict"] == "node-down"


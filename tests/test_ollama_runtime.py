"""Offline tests for the Ollama runtime the node carries.

Covers the download-button catalogue, the one-click bootstrap chain and the
typed ``ollama …`` command surface. Nothing here needs a running Ollama: the
HTTP layer is monkeypatched and every honest-failure path is asserted.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from core import api_catalog, ollama
from core.errors import OllamaUnavailable, ValidationError


# ------------------------------------------------------------ suggestions
def test_suggested_marks_installed_models():
    rows = ollama.suggested(model_rows=[{"name": "llama3.2:3b"}])
    by_name = {row["model"]: row for row in rows}
    assert by_name["llama3.2:3b"]["installed"] is True
    assert by_name["qwen2.5:7b-instruct"]["installed"] is False
    assert by_name["llama3.2:3b"]["run"] == "ollama run llama3.2:3b"
    assert all(row["size_gb"] > 0 for row in rows)
    assert any(row["tools"] for row in rows)


def test_suggested_never_claims_a_model_when_ollama_is_down(monkeypatch):
    def down():
        raise OllamaUnavailable("not answering")

    monkeypatch.setattr(ollama, "models", down)
    rows = ollama.suggested()
    assert rows
    assert not any(row["installed"] for row in rows)


# --------------------------------------------------------- command parser
def test_parse_command_reads_model_and_prompt():
    parsed = ollama.parse_command("ollama run llama3.2:3b summarise the chain")
    assert parsed["verb"] == "run"
    assert parsed["model"] == "llama3.2:3b"
    assert parsed["prompt"] == "summarise the chain"
    assert parsed["raw"].startswith("ollama run")


def test_parse_command_accepts_bare_lines_and_aliases():
    assert ollama.parse_command("pull llama3.2:3b")["verb"] == "pull"
    assert ollama.parse_command("ls")["verb"] == "list"
    assert ollama.parse_command("remove llama3.2:3b")["verb"] == "rm"
    assert ollama.parse_command("start")["verb"] == "serve"


def test_parse_command_rejects_empty_unknown_and_unsafe_models():
    with pytest.raises(ValidationError):
        ollama.parse_command("")
    with pytest.raises(ValidationError):
        ollama.parse_command("ollama explode")
    with pytest.raises(ValidationError):
        ollama.parse_command("ollama run ../../etc/passwd")


# ------------------------------------------------------------- run_cli
def test_run_cli_pull_returns_the_streaming_endpoint():
    out = ollama.run_cli("ollama pull llama3.2:3b")
    assert out["kind"] == "pull"
    assert out["model"] == "llama3.2:3b"
    assert out["stream"] == {"endpoint": "/api/ollama/pull", "method": "POST",
                             "body": {"model": "llama3.2:3b"}}


def test_run_cli_run_needs_a_prompt():
    with pytest.raises(ValidationError) as problem:
        ollama.run_cli("ollama run llama3.2:3b")
    assert "prompt" in str(problem.value)


def test_run_cli_run_generates_from_the_prompt(monkeypatch):
    seen = {}

    def fake_generate(model, prompt):
        seen["model"] = model
        seen["prompt"] = prompt
        return {"model": model, "response": "the chain is 23 blocks",
                "done_reason": "stop", "eval_count": 7}

    monkeypatch.setattr(ollama, "generate", fake_generate)
    out = ollama.run_cli("ollama run fake:1b how tall is the chain")
    assert seen == {"model": "fake:1b", "prompt": "how tall is the chain"}
    assert out["kind"] == "run"
    assert out["response"] == "the chain is 23 blocks"
    assert out["eval_count"] == 7


def test_run_cli_help_list_and_ps(monkeypatch):
    assert ollama.run_cli("ollama help")["kind"] == "help"
    monkeypatch.setattr(ollama, "models", lambda: [{"name": "fake:1b"}])
    listed = ollama.run_cli("ollama list")
    assert listed["count"] == 1 and listed["models"][0]["name"] == "fake:1b"
    monkeypatch.setattr(ollama, "running", lambda: [{"name": "fake:1b"}])
    assert ollama.run_cli("ollama ps")["models"] == [{"name": "fake:1b"}]


# ------------------------------------------------------------- bootstrap
def test_bootstrap_reports_a_failed_install(monkeypatch):
    monkeypatch.setattr(ollama, "install", lambda *a, **k: {
        "installed": False, "log": "no installer", "next": "install manually"})
    out = ollama.bootstrap()
    assert out["installed"] is False and out["serving"] is False
    assert out["steps"][0]["step"] == "install"
    assert out["next"] == "install manually"
    assert out["pulled"] is None


def test_bootstrap_starts_and_pulls_the_default_model(monkeypatch):
    monkeypatch.setattr(ollama, "install", lambda *a, **k: {
        "installed": True, "already": True, "version": "0.1"})
    monkeypatch.setattr(ollama, "start_server", lambda *a, **k: {
        "started": True, "already_running": False,
        "api_url": "http://127.0.0.1:11434", "version": "0.1"})
    state = {"rows": []}
    monkeypatch.setattr(ollama, "models", lambda: state["rows"])

    def fake_pull(model):
        state["rows"] = [{"name": model}]
        return {"model": model, "status": "success"}

    monkeypatch.setattr(ollama, "pull_blocking", fake_pull)
    out = ollama.bootstrap()
    assert out["installed"] is True and out["serving"] is True
    assert [step["step"] for step in out["steps"]] == ["install", "start", "pull"]
    assert out["pulled"]["model"] == str(ollama.settings.get("ollama.model"))
    assert out["default_model"] == str(ollama.settings.get("ollama.model"))


def test_ensure_runtime_is_honest_when_ollama_is_missing(monkeypatch):
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    monkeypatch.setattr(ollama, "discover_local", lambda *a, **k: None)
    out = ollama.ensure_runtime()
    assert out["serving"] is False and out["installed"] is False
    assert "bootstrap" in out["hint"]


def test_ensure_runtime_auto_installs_only_when_opted_in(monkeypatch):
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    monkeypatch.setattr(ollama, "install", lambda *a, **k: {
        "installed": True, "version": "0.1"})
    monkeypatch.setattr(ollama, "start_server", lambda *a, **k: {
        "started": True, "already_running": False,
        "api_url": "http://127.0.0.1:11434", "version": "0.1"})
    out = ollama.ensure_runtime(auto_install=True)
    assert out["serving"] is True and out["installed"] is True
    assert out["started"] is True


def test_ensure_runtime_reports_a_failed_auto_install(monkeypatch):
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    monkeypatch.setattr(ollama, "install", lambda *a, **k: {
        "installed": False, "next": "winget is missing"})
    out = ollama.ensure_runtime(auto_install=True)
    assert out["serving"] is False and out["installed"] is False
    assert out["error"] == "winget is missing"


# --------------------------------------------------------------- routes
def test_ollama_routes_answer_without_a_live_runtime(monkeypatch):
    from oneness_tor.app import create_app
    from oneness_tor.services import auth

    monkeypatch.setattr(ollama, "models", lambda: [])
    monkeypatch.setattr(auth, "is_admin", lambda token: True)
    client = create_app().test_client()

    suggested = client.get("/api/ollama/suggested").get_json()
    assert suggested["ok"] is True
    assert len(suggested["data"]["models"]) == len(ollama.SUGGESTED_MODELS)
    assert all(row["installed"] is False for row in suggested["data"]["models"])

    missing = client.post("/api/ollama/command", json={})
    assert missing.status_code == 400
    assert "command is required" in missing.get_json()["error"]["message"]

    help_reply = client.post("/api/ollama/command",
                             json={"command": "ollama help"}).get_json()
    assert help_reply["ok"] is True
    assert help_reply["data"]["kind"] == "help"

    monkeypatch.setattr(auth, "is_admin", lambda token: False)
    forbidden = client.post("/api/ollama/command", json={"command": "ollama help"})
    assert forbidden.status_code == 403


# ------------------------------------------------------------ hosted Ollama
def test_is_local_only_trusts_this_machine():
    assert ollama.is_local("http://127.0.0.1:11434") is True
    assert ollama.is_local("127.0.0.1:8080") is True
    assert ollama.is_local("localhost:11434") is True
    assert ollama.is_local("http://[::1]:11434") is True
    assert ollama.is_local("https://ollama.com") is False
    assert ollama.is_local("http://192.168.1.20:11434") is False


def test_api_key_reads_oneness_first_then_ollama(monkeypatch):
    for name in ollama.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    assert ollama.api_key() == ""
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-ollama")
    assert ollama.api_key() == "sk-ollama"
    monkeypatch.setenv("ONENESS_OLLAMA_API_KEY", "sk-oneness")
    assert ollama.api_key() == "sk-oneness"


def test_hosted_ollama_is_never_installed_or_started(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    touched = {"install": 0}

    def fake_install(*args, **kwargs):
        touched["install"] += 1
        return {"installed": True}

    monkeypatch.setattr(ollama, "install", fake_install)
    out = ollama.ensure_runtime(auto_install=True)
    assert out["remote"] is True and out["serving"] is False
    assert touched["install"] == 0
    assert "hosted" in out["error"]
    with pytest.raises(OllamaUnavailable) as problem:
        ollama.start_server(wait=1)
    assert "another machine" in str(problem.value)


def test_status_reports_a_hosted_brain_and_never_the_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "https://ollama.com")
    for name in ollama.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)

    def no_route(*args, **kwargs):
        raise OllamaUnavailable("no route to host")

    monkeypatch.setattr(ollama, "_request", no_route)
    monkeypatch.setattr(ollama, "version", lambda: None)
    out = ollama.status()
    assert out["api_url"] == "https://ollama.com"
    assert out["remote"] is True and out["api_key"] is False
    assert "hosted" in out["error"]
    assert "API_KEY" in out["error"]

    monkeypatch.setenv("OLLAMA_API_KEY", "sk-secret-value")
    with_key = ollama.status()
    assert with_key["api_key"] is True
    assert "sk-secret-value" not in json.dumps(with_key)


def test_status_points_at_a_local_ollama_on_another_port(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "http://127.0.0.1:11434")

    def refused(*args, **kwargs):
        raise OllamaUnavailable("connection refused")

    monkeypatch.setattr(ollama, "_request", refused)
    monkeypatch.setattr(ollama, "discover_local",
                        lambda *a, **k: "http://127.0.0.1:8080")
    out = ollama.status()
    assert out["remote"] is False and out["reachable"] is False
    assert "install" in out["error"]
    assert "ONENESS_OLLAMA_URL=http://127.0.0.1:8080" in out["error"]


def test_start_server_without_a_binary_names_the_hosted_exit(monkeypatch):
    """A box that cannot hold a model must not be told to install one."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    monkeypatch.setattr(ollama, "find_binary", lambda: None)
    monkeypatch.setattr(ollama, "discover_local", lambda *a, **k: None)
    with pytest.raises(OllamaUnavailable) as problem:
        ollama.start_server(wait=1)
    message = str(problem.value)
    assert "no `ollama` binary" in message
    assert "ONENESS_OLLAMA_URL=https://ollama.com" in message
    assert "API key is set" in message
    assert "sk-test" not in message
    assert problem.value.details["install"] == "POST /api/ollama/install"


def test_start_server_names_the_port_that_does_answer(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    monkeypatch.setattr(ollama, "find_binary", lambda: None)
    monkeypatch.setattr(ollama, "discover_local",
                        lambda *a, **k: "http://127.0.0.1:8080")
    with pytest.raises(OllamaUnavailable) as problem:
        ollama.start_server(wait=1)
    message = str(problem.value)
    assert "ONENESS_OLLAMA_URL=http://127.0.0.1:8080" in message
    assert "https://ollama.com" not in message      # the local answer wins


def test_a_box_that_cannot_run_a_model_adopts_the_hosted_exit(monkeypatch):
    """A 512 MB container with a token must not stay stuck on 127.0.0.1:11434."""
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    # "" is "unset" for every reader here, and (unlike delenv on a missing key)
    # pytest restores it after the test — adoption writes onto this same var.
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "")
    monkeypatch.setenv("ONENESS_OLLAMA_API_KEY", "sk-oneness")
    monkeypatch.setattr(ollama, "is_serving", lambda: False)
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    monkeypatch.setattr(ollama, "discover_local", lambda *a, **k: None)

    out = ollama.ensure_runtime(auto_install=False)
    assert out["remote"] is True and out["serving"] is False
    assert out["api_url"] == ollama.HOSTED_URL == "https://ollama.com"
    assert out["adopted_url"] == ollama.HOSTED_URL
    assert ollama.base_url() == "https://ollama.com"      # every later call too
    assert "bootstrap" not in out["error"]
    assert "not installed" not in out["error"].lower()
    assert "sk-oneness" not in json.dumps(out)


def test_adoption_never_overrides_a_chosen_address(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("ONENESS_OLLAMA_API_KEY", "sk-oneness")
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    monkeypatch.setattr(ollama, "discover_local", lambda *a, **k: None)
    assert ollama.unconfigured() is False
    assert ollama.adopt_hosted() is None
    assert ollama.base_url() == "http://127.0.0.1:8080"

    monkeypatch.delenv("ONENESS_OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11500")
    assert ollama.unconfigured() is False
    assert ollama.adopt_hosted() is None
    assert ollama.base_url() == "http://127.0.0.1:11500"


def test_adoption_needs_a_token_and_no_local_ollama(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "")   # "" reads as unset, and restores
    for name in ollama.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    monkeypatch.setattr(ollama, "discover_local", lambda *a, **k: None)
    assert ollama.unconfigured() is True
    assert ollama.adopt_hosted() is None          # no token: explain, never switch

    monkeypatch.setenv("ONENESS_OLLAMA_API_KEY", "sk-oneness")
    monkeypatch.setattr(ollama, "is_installed", lambda: True)
    assert ollama.adopt_hosted() is None          # a laptop that has Ollama keeps it
    monkeypatch.setattr(ollama, "discover_local",
                        lambda *a, **k: "http://127.0.0.1:8080")
    monkeypatch.setattr(ollama, "is_installed", lambda: False)
    assert ollama.adopt_hosted() is None          # an Ollama that answers wins
    monkeypatch.setattr(ollama, "discover_local", lambda *a, **k: None)
    assert ollama.adopt_hosted(auto_install=True) is None   # "install here" wins
    assert ollama.adopt_hosted() == ollama.HOSTED_URL


def test_discover_local_never_probes_a_hosted_host(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "https://ollama.com")

    def explode(*args, **kwargs):
        raise AssertionError("a hosted Ollama must never be probed on loopback")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", explode)
    assert ollama.discover_local() is None
    assert ollama.port_hint() == ""


# --------------------------------------------------------------- catalog
def test_catalog_exposes_the_runtime_endpoints_in_the_prompt():
    names = api_catalog.names()
    for name in ("ollama.suggested", "ollama.bootstrap", "ollama.command"):
        assert name in names, name
    text = api_catalog.prompt_text("http://127.0.0.1:8090")
    assert "ollama.command" in text
    assert "ollama.bootstrap" in text



def test_bearer_token_is_sent_and_a_401_says_what_to_fix(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-secret")
    seen = {}

    def unauthorized(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":"invalid api key"}'))

    monkeypatch.setattr(ollama.urllib.request, "urlopen", unauthorized)
    with pytest.raises(OllamaUnavailable) as problem:
        ollama._request("/api/version")
    assert seen["auth"] == "Bearer sk-secret"
    message = str(problem.value)
    assert "the API key was rejected" in message
    assert "sk-secret" not in message

    for name in ollama.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(OllamaUnavailable) as missing:
        ollama._request("/api/version")
    assert "needs an API key" in str(missing.value)


def test_health_reports_the_ollama_config_without_probing(monkeypatch):
    from core import ledger_host
    from core.node_client import node
    from oneness_tor.app import create_app

    monkeypatch.setattr(node, "is_reachable", lambda: False)
    monkeypatch.setattr(ledger_host, "status",
                        lambda: {"running": False, "hint": "start the node"})
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "https://ollama.com")
    monkeypatch.setenv("ONENESS_OLLAMA_API_KEY", "sk-secret")

    def explode(*args, **kwargs):
        raise AssertionError("health must not probe Ollama")

    monkeypatch.setattr(ollama, "_request", explode)
    body = create_app().test_client().get("/api/health").get_json()["data"]
    assert body["ollama"] == {"api_url": "https://ollama.com", "remote": True,
                              "api_key": True,
                              "model": str(ollama.settings.get("ollama.model"))}
    assert "sk-secret" not in json.dumps(body)

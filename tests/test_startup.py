"""The one start command: bind resolution, the Ollama bootstrap and its reporting.

Offline: no installer runs and no model is pulled — every side effect is stubbed
so the *decisions* (which host/port, install or not, pull or not) are asserted.
"""
from __future__ import annotations

from core import ollama
from oneness_tor import startup


def test_resolve_bind_prefers_flags_then_port_then_config(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("ONENESS_API_HOST", raising=False)
    assert startup.resolve_bind("0.0.0.0", 9100) == ("0.0.0.0", 9100)
    assert startup.resolve_bind(None, 8091) == ("127.0.0.1", 8091)

    monkeypatch.setenv("PORT", "10000")          # Render injects $PORT
    assert startup.resolve_bind() == ("0.0.0.0", 10000)

    monkeypatch.setenv("ONENESS_API_HOST", "127.0.0.1")
    assert startup.resolve_bind() == ("127.0.0.1", 10000)


def test_resolve_bind_never_crashes_on_a_bad_port(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert startup.resolve_bind(None, "not-a-port")[1] == startup.DEFAULT_PORT


def test_ollama_install_dir_and_binary_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_INSTALL_DIR", str(tmp_path / "ollama"))
    assert ollama.install_dir() == tmp_path / "ollama"

    binary = tmp_path / "ollama"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("OLLAMA_BIN", str(binary))
    assert ollama.find_binary() == str(binary)


def test_serve_env_binds_where_the_api_expects(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("ONENESS_OLLAMA_URL", "http://127.0.0.1:11500")
    assert ollama._serve_env()["OLLAMA_HOST"] == "127.0.0.1:11500"

    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert ollama._serve_env()["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_the_start_command_installs_but_does_not_pull_unless_asked(monkeypatch):
    calls: dict = {"installed": 0, "started": 0, "pulled": 0}

    def ensure_runtime(wait=20.0, *, auto_install=False):
        calls["installed"] += int(bool(auto_install))
        calls["started"] += 1
        return {"serving": True, "installed": True, "started": True,
                "api_url": "http://127.0.0.1:11434", "version": "0.1"}

    monkeypatch.setattr(ollama, "ensure_runtime", ensure_runtime)
    monkeypatch.setattr(ollama, "models", lambda: [])
    monkeypatch.setattr(ollama, "pull_blocking",
                        lambda model: calls.__setitem__("pulled", calls["pulled"] + 1)
                        or {"model": model, "status": "success"})

    quiet = startup.ollama_runtime(auto_install=True, pull=False, wait=1.0)
    assert quiet["serving"] is True and quiet["pulled"] is None
    assert calls == {"installed": 1, "started": 1, "pulled": 0}

    loud = startup.ollama_runtime(auto_install=True, pull=True, wait=1.0)
    assert loud["pulled"]["status"] == "success" and calls["pulled"] == 1


def test_a_hosted_brain_is_never_told_to_install(capsys):
    startup.print_runtime({"serving": False, "installed": False, "started": False,
                           "remote": True, "api_url": "https://ollama.com",
                           "error": "no Ollama is answering at https://ollama.com"})
    text = capsys.readouterr().out
    assert "hosted https://ollama.com" in text
    assert "API_KEY" in text                   # the missing key is named once
    assert "bootstrap" not in text
    assert ollama.DOWNLOAD_URL not in text
    assert "NOT installed" not in text


def test_an_adopted_hosted_brain_says_why(capsys):
    startup.print_runtime({"serving": True, "installed": False, "started": False,
                           "remote": True, "api_url": "https://ollama.com",
                           "adopted_url": "https://ollama.com",
                           "version": "0.12.3"})
    text = capsys.readouterr().out
    assert "https://ollama.com (hosted, v0.12.3)" in text
    assert "adopted the hosted brain" in text
    assert "NOT installed" not in text
    assert ollama.DOWNLOAD_URL not in text
    assert "bootstrap" not in text


def test_a_failed_install_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_runtime",
                        lambda wait=20.0, auto_install=False: {
                            "serving": False, "installed": False, "started": False,
                            "api_url": "http://127.0.0.1:11434",
                            "error": "curl is missing"})
    runtime = startup.ollama_runtime(auto_install=True, pull=True, wait=1.0)
    assert runtime["serving"] is False and runtime["error"] == "curl is missing"
    assert runtime["pulled"] is None

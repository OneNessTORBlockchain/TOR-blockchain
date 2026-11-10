"""Offline tests: catalog integrity, the fetch-data tool and the Ollama helpers.

No node and no Ollama are required — anything that would need them asserts the
*honest failure* path instead (``TOOL_UNREACHABLE`` / ``OLLAMA_UNAVAILABLE``).
"""
from __future__ import annotations

import json

import pytest

from core import api_catalog, ollama
from core.errors import ValidationError
from core.tools import ToolBox, summarize
from oneness_tor.services import builder
from oneness_tor.services.builder import _extract_html


# ------------------------------------------------------------------ catalog
def test_names_are_unique_and_tool_spec_covers_them():
    names = api_catalog.names()
    assert len(names) == len(set(names))
    spec = api_catalog.tool_spec()["function"]
    assert spec["name"] == "fetch_oneness_data"
    assert spec["parameters"]["properties"]["endpoint"]["enum"] == names
    assert spec["parameters"]["required"] == ["endpoint"]


def test_groups_partition_every_endpoint():
    groups = api_catalog.groups()
    assert sum(group["count"] for group in groups) == len(api_catalog.ALL)
    assert {group["name"] for group in groups} == {entry["group"] for entry in api_catalog.ALL}


def test_prompt_text_lists_every_endpoint_and_its_return_shape():
    text = api_catalog.prompt_text("http://127.0.0.1:8090")
    for entry in api_catalog.ALL:
        assert entry["name"] in text, entry["name"]
    assert "fetch_oneness_data" in text
    assert "http://127.0.0.1:8090" in text
    # paid rows must be announced with their price
    assert "meme.landing 10" in text
    assert "contracts.create 5" in text


def test_openapi_document_matches_the_registry():
    document = api_catalog.openapi("http://127.0.0.1:8090")
    assert document["openapi"] == "3.1.0"
    operations = sum(len(ops) for ops in document["paths"].values())
    assert operations == len(api_catalog.ALL)
    assert "get" in document["paths"]["/api/chain/info"]
    assert document["servers"][0]["url"] == "http://127.0.0.1:8090"


def test_admin_endpoints_are_marked_as_such():
    for entry in api_catalog.ALL:
        if entry["name"] in ("admin.archive", "ollama.install", "wallets.list"):
            assert entry["auth"] == "admin", entry["name"]


def test_paid_public_actions_are_marked_wallet_session():
    """Mints, builds and swaps are paid by the visitor's own wallet, not a token."""
    for entry in api_catalog.ALL:
        if entry["name"] in ("nft.mint", "meme.token_create", "meme.landing",
                             "contracts.create"):
            assert entry["auth"] == "wallet-session-or-admin", entry["name"]


def test_by_name_returns_none_for_an_unknown_endpoint():
    assert api_catalog.by_name("chain.info")["method"] == "GET"
    assert api_catalog.by_name("nope.nope") is None


def test_catalog_copies_are_defensive():
    entry = api_catalog.by_name("chain.blocks")
    entry["params"].clear()
    assert api_catalog.by_name("chain.blocks")["params"], "registry was mutated"


# ------------------------------------------------------------------ tool box
def test_toolbox_rejects_an_unknown_endpoint():
    box = ToolBox("http://127.0.0.1:9")
    with pytest.raises(ValidationError):
        box.execute("nope.nope")


def test_toolbox_requires_mandatory_params():
    box = ToolBox("http://127.0.0.1:9")
    with pytest.raises(ValidationError):
        box.execute("account.get", {})


def test_toolbox_reports_unreachable_api_honestly():
    box = ToolBox("http://127.0.0.1:9", timeout=2)
    result = box.execute("chain.info")
    assert result["ok"] is False
    assert result["error"]["code"] == "TOOL_UNREACHABLE"
    assert result["http_status"] == 0
    assert "not answering" in result["error"]["message"]


def test_toolbox_builds_path_query_and_body_from_params():
    box = ToolBox("http://127.0.0.1:9")
    path, query, body = box._build(api_catalog.by_name("chain.blocks"),
                                   {"from": "3", "to": 9, "limit": "5"})
    assert path == "/api/chain/blocks"
    assert query == {"from": 3, "to": 9, "limit": "5"}   # ints coerced, unknown kept
    path, query, body = box._build(api_catalog.by_name("account.get"),
                                   {"address": "TOR1abc.onion"})
    assert path.endswith("/TOR1abc.onion") and body == {}


def test_summarize_trims_big_payloads_without_faking_them():
    big = {"ok": True, "endpoint": "records.public", "http_status": 200,
           "data": {"records": [{"txid": "x" * 500} for _ in range(50)]}}
    text = summarize(big, limit=600)
    assert len(text) <= 600
    assert '"endpoint":"records.public"' in text


# -------------------------------------------------------------------- ollama
def test_base_url_honours_ollama_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:8080")
    assert ollama.base_url() == "http://127.0.0.1:8080"
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434/")
    assert ollama.base_url() == "http://gpu-box:11434"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert ollama.base_url() == ollama.DEFAULT_URL
    monkeypatch.delenv("OLLAMA_HOST")
    assert ollama.base_url().startswith("http://")


def test_pick_model_prefers_configured_then_local():
    rows = [{"name": "cloud/thing:cloud"}, {"name": "llama3.2:3b"}]
    assert ollama.pick_model("explicit:1b", model_rows=rows) == "explicit:1b"
    assert ollama.pick_model(None, model_rows=rows) == "llama3.2:3b"
    assert ollama.pick_model(None, model_rows=[]) == str(
        ollama.settings.get("ollama.model"))


def test_pseudo_calls_extract_json_from_a_plain_answer():
    text = ('I will check. {"endpoint": "chain.info", "params": {"limit": 3}} '
            "then answer.")
    calls = ollama._pseudo_calls(text)
    assert len(calls) == 1
    name, arguments = ollama._call_parts(calls[0])
    assert name == "fetch_oneness_data"
    assert arguments == {"endpoint": "chain.info", "params": {"limit": 3}}


def test_call_parts_accepts_string_arguments():
    name, arguments = ollama._call_parts({
        "function": {"name": "fetch_oneness_data",
                     "arguments": json.dumps({"endpoint": "wallets.list"})}})
    assert name == "fetch_oneness_data"
    assert arguments["endpoint"] == "wallets.list"


def test_pseudo_calls_ignores_other_json():
    assert ollama._pseudo_calls('{"ok": true, "data": 1}') == []


def test_pseudo_calls_survives_sloppy_json():
    text = ('```json\n{"endpoint": "chain.info", "params": { chain_height: 30, }}\n```')
    calls = ollama._pseudo_calls(text)
    assert len(calls) == 1
    _, arguments = ollama._call_parts(calls[0])
    assert arguments["endpoint"] == "chain.info"
    assert arguments["params"] == {"chain_height": 30}

    _, clean = ollama._call_parts(
        ollama._pseudo_calls('{"endpoint": "chain.blocks", "params": {"from": 0}}')[0])
    assert clean["params"] == {"from": 0}


def test_loose_params_quotes_keys_and_drops_trailing_commas():
    assert ollama._loose_params("{amount_tor: 3, note: 'x',}") == {
        "amount_tor": 3, "note": "x"}


def test_chat_for_falls_back_when_the_model_lacks_tools(monkeypatch):
    from core.errors import OllamaUnavailable

    seen: list = []

    def fake_chat(messages, model, *, tools=None, options=None):
        seen.append(tools)
        if tools:
            raise OllamaUnavailable(
                "Ollama said HTTP 400: registry.ollama.ai/library/tiny:1b does not "
                "support tools")
        return {"message": {"content": "plain answer"}}

    monkeypatch.setattr(ollama, "chat", fake_chat)
    ollama.TOOL_UNSUPPORTED.discard("tiny:1b")
    reply = ollama._chat_for("tiny:1b", [{"role": "user", "content": "hi"}],
                             [{"type": "function"}])
    assert reply["message"]["content"] == "plain answer"
    assert "tiny:1b" in ollama.TOOL_UNSUPPORTED
    assert seen[0] is not None and seen[-1] is None


def test_run_command_executes_the_tool_then_answers(monkeypatch):
    class FakeBox:
        base_url = "http://127.0.0.1:9"

        def __init__(self):
            self.spec = api_catalog.tool_spec()

        def execute(self, endpoint, params=None, **kwargs):
            return {"ok": True, "endpoint": endpoint, "http_status": 200,
                    "latency_ms": 1,
                    "request": {"method": "GET", "url": "http://x/api/chain/info"},
                    "data": {"height": 7}, "error": None}

    replies = iter([
        {"message": {"content": '{"endpoint": "chain.info", "params": {}}'}},
        {"message": {"content": "The chain height is 7."}},
    ])
    monkeypatch.setattr(ollama, "chat", lambda *a, **k: next(replies))
    result = ollama.run_command("what is the height?", model="fake:1b",
                                toolbox=FakeBox())
    assert result["api_calls"][0]["endpoint"] == "chain.info"
    assert result["api_calls"][0]["http_status"] == 200
    assert result["answer"] == "The chain height is 7."


# ------------------------------------------------------------------- builder
def test_extract_html_strips_fences_and_trailing_prose():
    raw = "```html\n<!DOCTYPE html><html><body>hi</body></html>\n```\ndone"
    assert _extract_html(raw).startswith("<!DOCTYPE html>")
    assert _extract_html(raw).endswith("</html>")


def test_extract_html_rejects_plain_prose():
    with pytest.raises(ValidationError):
        _extract_html("Sure, here is a site: it is very neon.")


def test_extract_html_rejects_a_page_cut_off_before_the_closing_tag():
    with pytest.raises(ValidationError) as problem:
        _extract_html("<!DOCTYPE html><html><body>half a page")
    assert "cut off" in problem.value.message


def test_builder_switches_reasoning_off_for_a_thinking_model(monkeypatch):
    """A reasoning trace used to consume the whole budget: ``response`` empty."""
    seen: dict = {}

    def fake_generate(model, prompt, **kwargs):
        seen.update(kwargs)
        return {"response": "<!DOCTYPE html><html><body>neon</body></html>",
            "model": model, "done_reason": "stop"}

    monkeypatch.setattr(builder.ollama, "supports_thinking", lambda model: True)
    monkeypatch.setattr(builder.ollama, "generate", fake_generate)
    page = builder.generate("neon coffee shop", title="Neon Brew", model="deepseek:cloud")
    assert seen["think"] is False
    assert page["html"].startswith("<!DOCTYPE html>")


def test_builder_retries_when_the_page_was_cut_off(monkeypatch):
    calls: list[int] = []

    def fake_generate(model, prompt, **kwargs):
        calls.append(kwargs["options"]["num_predict"])
        if len(calls) == 1:
            return {"response": "<!DOCTYPE html><html><body>cut", "model": model,
                    "done_reason": "length"}
        return {"response": "<!DOCTYPE html><html><body>whole</body></html>",
                "model": model, "done_reason": "stop"}

    monkeypatch.setattr(builder.ollama, "supports_thinking", lambda model: False)
    monkeypatch.setattr(builder.ollama, "generate", fake_generate)
    page = builder.generate("neon shop", model="gemma3:1b")
    assert calls == [4096, 16384]
    assert "whole" in page["html"]


def test_builder_explains_a_model_that_only_thinks(monkeypatch):
    monkeypatch.setattr(builder.ollama, "supports_thinking", lambda model: True)
    monkeypatch.setattr(builder.ollama, "generate",
                        lambda *args, **kwargs: {"response": "", "thinking": "y" * 400,
                                                 "model": args[0],
                                                 "done_reason": "length"})
    with pytest.raises(ValidationError) as problem:
        builder.generate("neon shop", model="deepseek:cloud")
    assert "thinking" in problem.value.message

"""Ollama runtime, the fetch-data tool, the command brain and the docs."""
from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request, stream_with_context

from core import api_catalog, ledger_host, ollama
from core import nft as nft_meta
from core.config import settings
from core.errors import AppError, ChainUnavailable, ValidationError
from core.node_client import node
from core.tools import ToolBox
from oneness_tor.services import base_url, nft_studio
from oneness_tor.services.auth import require_admin, require_member, token_from_request

bp = Blueprint("brain_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return payload


def _toolbox() -> ToolBox:
    """Points the tool at *this* server so the call is a real HTTP round-trip.

    It must use ``base_url.internal_base()`` (loopback), not the URL the browser
    used: a Tor visitor's host is ``<id>.onion``, which the server cannot
    resolve (no SOCKS proxy) — that turned every tool call into
    ``TOOL_UNREACHABLE`` for onion users. ``display_base`` only affects what the
    catalog advertises, so the docs page shows the URL the visitor came in on.
    """
    return ToolBox(base_url.internal_base(),
                   display_base=base_url.request_base(request),
                   admin_token=token_from_request(request))


# ------------------------------------------------------------------ ollama
@bp.get("/ollama/status")
def ollama_status():
    return envelope(ollama.status())


@bp.get("/ollama/models")
def ollama_models():
    try:
        rows = ollama.models()
        return envelope({"models": rows, "count": len(rows),
                         "default_model": ollama.pick_model(model_rows=rows),
                         "api_url": ollama.base_url()})
    except Exception as problem:  # node must answer even when Ollama is down
        return envelope({"models": [], "count": 0,
                         "default_model": str(settings.ollama_model),
                         "api_url": ollama.base_url(),
                         "error": getattr(problem, "message", str(problem))})


@bp.get("/ollama/ps")
def ollama_ps():
    try:
        return envelope({"models": ollama.running()})
    except Exception as problem:
        return envelope({"models": [],
                         "error": getattr(problem, "message", str(problem))})


@bp.get("/ollama/prompt")
def ollama_prompt():
    text = ollama.build_system_prompt(_toolbox())
    return envelope({"system_prompt": text, "endpoint_count": len(api_catalog.ALL),
                     "model": ollama.pick_model(), "api_url": ollama.base_url(),
                     "characters": len(text)})


@bp.get("/ollama/routing")
def ollama_routing():
    """Which model each AI surface uses (brain, NFT, web builder, rust builder)."""
    return envelope(ollama.routing())


@bp.post("/ollama/routing")
def ollama_routing_set():
    """Point one surface at an installed model — or clear the override."""
    require_admin(request)
    payload = _body()
    return envelope(ollama.set_routing(payload.get("purpose"),
                                       payload.get("model")))


@bp.post("/ollama/install")
def ollama_install():
    require_admin(request)
    return envelope(ollama.install())


@bp.get("/ollama/suggested")
def ollama_suggested():
    """The download buttons: curated models + what is already on disk."""
    try:
        rows = ollama.models()
    except Exception:  # Ollama down: still answer honestly, nothing installed
        rows = []
    return envelope({"models": ollama.suggested(model_rows=rows),
                     "count": len(rows)})


@bp.post("/ollama/bootstrap")
def ollama_bootstrap():
    """Install → start → first model. One call, real steps reported."""
    require_admin(request)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return envelope(ollama.bootstrap(
        pull=bool(payload.get("pull", True)),
        model=(str(payload.get("model")).strip() if payload.get("model") else None)))


@bp.post("/ollama/command")
def ollama_command():
    """Run a typed `ollama …` line (`run`, `pull`, `list`, `ps`, `show`, `rm`)."""
    require_admin(request)
    payload = _body()
    command = str(payload.get("command") or "").strip()
    if not command:
        raise ValidationError("command is required, e.g. " +
                              "`ollama run llama3.2:3b hello`",
                              details={"examples": ollama.CLI_HELP})
    prompt = payload.get("prompt")
    return envelope(ollama.run_cli(
        command, prompt=str(prompt).strip() if prompt else None))


@bp.post("/ollama/start")
def ollama_start():
    require_admin(request)
    return envelope(ollama.start_server())


@bp.post("/ollama/pull")
def ollama_pull():
    require_admin(request)
    payload = _body()
    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValidationError("model is required, e.g. llama3.2:3b")

    def stream():
        try:
            for chunk in ollama.pull(model):
                yield json.dumps({"ok": True, "model": model, **chunk}) + "\n"
                if chunk.get("error"):  # never claim success after an error chunk
                    return
            yield json.dumps({"ok": True, "model": model, "status": "success",
                              "done": True}) + "\n"
        except AppError as problem:
            yield json.dumps({"ok": False, "error": problem.to_error()}) + "\n"
        except Exception as problem:  # pragma: no cover - defensive
            yield json.dumps({"ok": False, "error": {
                "code": "INTERNAL", "message": str(problem)[:300]}}) + "\n"

    return Response(stream_with_context(stream()),
                    mimetype="application/x-ndjson",
                    headers={"cache-control": "no-store",
                             "x-accel-buffering": "no"})


@bp.delete("/ollama/models/<path:model>")
def ollama_delete(model: str):
    require_admin(request)
    return envelope(ollama.delete(model))


@bp.post("/ollama/run")
def ollama_run():
    require_admin(request)
    payload = _body()
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValidationError("prompt is required")
    chosen = ollama.pick_model(payload.get("model"))
    return envelope(ollama.run(chosen, prompt))


# ------------------------------------------------------------- command deck
def _command(*, force_tools: bool = False):
    require_member(request)
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    messages = payload.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise ValidationError("messages must be a list of {role, content}")
    if not prompt and not messages:
        raise ValidationError("prompt (or messages) is required")
    use_tools = True if force_tools else bool(payload.get("use_tools", True))
    nft_request = payload.get("nft")
    nft_record: dict | None = None
    if isinstance(nft_request, dict):
        nft_record = nft_studio.load(nft_request.get("address"),
                                     nft_request.get("token_id"),
                                     nft_request.get("metadata_uri", ""))
    elif payload.get("nft_address"):
        nft_record = nft_studio.load(payload.get("nft_address"),
                                     payload.get("token_id"))
    result = ollama.run_command(prompt or None, model=payload.get("model"),
                                messages=messages, toolbox=_toolbox(),
                                use_tools=use_tools, nft=nft_record,
                                token_id=(nft_record or {}).get("token_id"))
    transcript = [message for message in result["messages"]
                  if message.get("role") != "system"]
    system = result["messages"][0]["content"] if result["messages"] else ""
    return envelope({"answer": result["answer"], "model": result["model"],
                     "tool_calls": result["tool_calls"],
                     "api_calls": result["api_calls"],
                     "rounds": result["rounds"], "use_tools": use_tools,
                     "native_tools": result.get("native_tools", True),
                     "verified": result.get("verified", bool(result["api_calls"])),
                     "messages": transcript,
                     "nft": nft_meta.summary(nft_record) if nft_record else None,
                     "nft_prompt": (nft_meta.persona_block(
                         nft_record, nft_record.get("token_id"))
                         if nft_record else None),
                     "system_prompt_chars": len(system),
                     "endpoint_count": len(api_catalog.ALL)})


@bp.post("/ollama/chat")
def ollama_chat():
    return _command()


@bp.post("/ai/command")
def ai_command():
    return _command(force_tools=True)


# ------------------------------------------------------------------- tools
@bp.get("/tools")
def tools_catalog():
    toolbox = _toolbox()
    return envelope({**toolbox.describe(), "spec": toolbox.spec,
                     "version": api_catalog.CATALOG_VERSION,
                     "groups": api_catalog.groups()})


@bp.post("/tools/fetch")
def tools_fetch():
    payload = _body()
    endpoint = str(payload.get("endpoint") or "").strip()
    if not endpoint:
        raise ValidationError("endpoint is required (see GET /api/tools)")
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValidationError("params must be an object")
    result = _toolbox().execute(endpoint, params, token=token_from_request(request))
    return envelope(result, tool="fetch_oneness_data")


# -------------------------------------------------------------------- docs
@bp.get("/docs")
def docs_catalog():
    base = base_url.request_base(request)
    document = api_catalog.catalog_document(base)
    document["endpoints"] = [{**entry, "url": f"{base}{entry['path']}"}
                             for entry in api_catalog.endpoints()]
    document["base_urls"] = base_url.base_urls(request)
    try:
        reachable = node.is_reachable()
        info = node.info().get("data") if reachable else None
    except ChainUnavailable:
        reachable, info = False, None
    document["node"] = {"reachable": reachable, "base_url": node.base_url,
                        "chain": info,
                        # why it is down and what this process did about it, so
                        # the page can say it instead of a bare "node down"
                        "managed": ledger_host.status().get("managed"),
                        "hint": None if reachable
                        else ledger_host.status().get("hint")}
    document["tool"] = api_catalog.tool_spec()["function"]["name"]
    return envelope(document)


@bp.get("/docs/endpoints")
def docs_endpoints():
    base = base_url.request_base(request)
    return envelope({"endpoints": [{**entry, "url": f"{base}{entry['path']}"}
                                   for entry in api_catalog.endpoints()],
                     "base_url": base,
                     "count": len(api_catalog.ALL),
                     "version": api_catalog.CATALOG_VERSION})


@bp.get("/openapi.json")
def openapi_document():
    return jsonify(api_catalog.openapi(base_url.request_base(request)))

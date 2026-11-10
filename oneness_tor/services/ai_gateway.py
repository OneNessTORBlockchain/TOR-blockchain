"""OpenRouter gateway — one ChatBox model list for every TOR platform.

The AI-NFT ChatBox on TOR Onion Swap, TOR-Screener, DarkWeb Crypto, TOR Pump and
the Solarix dashboard all call this node (``POST /api/ai/chat``), never
OpenRouter directly: the key stays server-side, and the dropdown is filled from
:func:`models`, which lists **only** the free models (``id`` ending in ``:free``
or a zero prompt+completion price) that OpenRouter is currently serving.

Nothing is invented: when OpenRouter cannot be reached the answer carries the
real error and the bundled fallback list is marked ``source: "bundled"``.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from core.config import settings
from core.errors import AppError

CACHE: dict[str, object] = {"at": 0.0, "models": [], "source": "none", "error": None}
LOCK = threading.RLock()
FREE_TTL = 600.0

#: A last-resort list (real OpenRouter ids) so a Tor-only browser still gets a
#: usable dropdown when the gateway is unreachable from the node.
FALLBACK_MODELS = [
    "openrouter/free",
    "cohere/north-mini-code:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "recursive/flint-v1.0:free",
    "thinkingmachines/inkling:free",
    "xingchen-agi/xing4.0-29b-a4b:free",
]

DEFAULT_SYSTEM = (
    "You are the TOR AI-NFT assistant for the OneNess TOR Blockchain — a "
    "privacy-first chain reachable at "
    "http://4thdw6cvoacqtxpyvxziebiif5qmiwginb7wwgktsalmevrabqubpxid.onion and "
    "www.onenesstorblockchain.com. TOR is quoted at $3.60 and every token price "
    "comes from a real AMM pool on the chain. Be concise, technical and honest: "
    "never invent a price, a balance or an endpoint — say what the node reports."
)


def _key() -> str:
    return str(settings.get("openrouter.api_key") or "").strip()


def _base() -> str:
    return str(settings.get("openrouter.base_url")
               or "https://openrouter.ai/api/v1").rstrip("/")


def _timeout() -> float:
    try:
        return float(settings.get("openrouter.timeout") or 90.0)
    except (TypeError, ValueError):
        return 90.0


def _request(path: str, payload: dict | None = None, *, method: str = "GET",
             key: str | None = None) -> dict:
    key = key if key is not None else _key()
    if not key:
        raise AppError("OpenRouter is not configured on this node "
                       "(set ONENESS_OPENROUTER_API_KEY)",
                       code="OPENROUTER_NOT_CONFIGURED", http_status=503)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{_base()}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": str(settings.get("openrouter.referer") or ""),
                 "X-Title": str(settings.get("openrouter.title") or "TOR")})
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as problem:
        detail = problem.read().decode("utf-8", "replace")[:400]
        raise AppError(f"OpenRouter answered {problem.code}",
                       code="OPENROUTER_ERROR", http_status=502,
                       details={"status": problem.code, "body": detail})
    except (urllib.error.URLError, TimeoutError, OSError) as problem:
        raise AppError(f"OpenRouter is unreachable: {problem}",
                       code="OPENROUTER_UNREACHABLE", http_status=502)


def _free(entry: dict) -> bool:
    model_id = str(entry.get("id") or "")
    if model_id.endswith(":free"):
        return True
    pricing = entry.get("pricing") or {}
    if not isinstance(pricing, dict):
        return False
    try:
        return (float(pricing.get("prompt") or 0) == 0.0
                and float(pricing.get("completion") or 0) == 0.0)
    except (TypeError, ValueError):
        return False

def _row(entry: dict) -> dict:
    architecture = entry.get("architecture") or {}
    outputs = architecture.get("output_modalities") or []
    context = entry.get("context_length")
    return {"id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or entry.get("id") or ""),
            "context_length": int(context) if isinstance(context, int) else None,
            "modality": architecture.get("modality"),
            "text_out": ("text" in outputs) if isinstance(outputs, list) else True,
            "description": str(entry.get("description") or "")[:280],
            "free": True}


def models(*, refresh: bool = False) -> dict:
    """The free model list the ChatBox dropdown renders (cached 10 minutes)."""
    with LOCK:
        age = time.time() - float(CACHE.get("at") or 0)
        cached = list(CACHE.get("models") or [])       # type: ignore[arg-type]
        if cached and not refresh and age < FREE_TTL:
            return {"models": cached, "count": len(cached),
                    "source": CACHE.get("source"), "error": CACHE.get("error"),
                    "default": str(settings.get("openrouter.model") or ""),
                    "cached": True}
        error = None
        try:
            payload = _request("/models")
            entries = payload.get("data") if isinstance(payload, dict) else None
            rows = [_row(entry) for entry in (entries or [])
                    if isinstance(entry, dict) and _free(entry)]
            rows = [row for row in rows
                    if row["text_out"] and row["id"] and "lyria" not in row["id"]]
            rows.sort(key=lambda row: row["id"])
            source = "openrouter"
            if not rows:
                raise AppError("OpenRouter returned no free text models",
                               code="OPENROUTER_EMPTY", http_status=502)
        except AppError as problem:
            error = problem.message
            rows = [{"id": model_id, "name": model_id, "context_length": None,
                     "modality": "text->text", "text_out": True,
                     "description": "bundled fallback (gateway unreachable)",
                     "free": True} for model_id in FALLBACK_MODELS]
            source = "bundled"
        CACHE.update({"at": time.time(), "models": rows, "source": source,
                      "error": error})
        return {"models": rows, "count": len(rows), "source": source,
                "error": error,
                "default": str(settings.get("openrouter.model") or ""),
                "cached": False}



def _threads(messages: object, prompt: object) -> list[dict]:
    rows: list[dict] = []
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role in ("system", "user", "assistant") and content:
                rows.append({"role": role, "content": content[:8000]})
    text = str(prompt or "").strip()
    if text:
        rows.append({"role": "user", "content": text[:8000]})
    return rows


def persona(nft: dict | None = None) -> str:
    """The system prompt: node facts, plus the NFT's own metadata when given."""
    blocks = [DEFAULT_SYSTEM]
    if isinstance(nft, dict) and nft:
        try:
            from core import nft as nft_meta

            blocks.append(nft_meta.persona_block(nft, nft.get("token_id")))
        except Exception:  # noqa: BLE001 - a persona is never fatal
            name = str(nft.get("name") or nft.get("token_id") or "this NFT")
            blocks.append(f"You are speaking as the AI-NFT {name}.")
    return "\n\n".join(blocks)


def chat(*, prompt: object = "", messages: object = None,
         model: object = "", system: object = "", nft: dict | None = None,
         temperature: object = 0.6, max_tokens: object = 700) -> dict:
    """One completion through OpenRouter, with the TOR persona prepended."""
    threads = _threads(messages, prompt)
    if not threads:
        raise AppError("prompt (or messages) is required",
                       code="VALIDATION_ERROR", http_status=400)
    chosen = str(model or settings.get("openrouter.model") or "").strip()
    available = {row["id"] for row in models()["models"]}
    if chosen and available and chosen not in available:
        raise AppError(f"{chosen} is not a free model on this gateway",
                       code="MODEL_NOT_FREE", http_status=400,
                       details={"free": sorted(available)[:40]})
    system_prompt = str(system or "").strip() or persona(nft)
    body = {"model": chosen,
            "messages": [{"role": "system", "content": system_prompt}] + threads,
            "temperature": float(temperature or 0.6),
            "max_tokens": int(max_tokens or 700)}
    reply = _request("/chat/completions", body, method="POST")
    choices = reply.get("choices") or []
    answer = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        answer = str(message.get("content") or "").strip()
    return {"answer": answer, "model": reply.get("model") or chosen,
            "mode": "ai-nft-chatbox", "provider": "openrouter", "free": True,
            "usage": reply.get("usage") or {}, "cost_usd": 0.0,
            "system_prompt_chars": len(system_prompt), "gateway": _base()}


def status() -> dict:
    """Readiness for the node's health page — never the key itself."""
    info = models()
    return {"configured": bool(_key()), "gateway": _base(),
            "default_model": str(settings.get("openrouter.model") or ""),
            "free_models": info["count"], "source": info["source"],
            "error": info["error"],
            "note": "free models only; the key never leaves this node"}


"""Ollama runtime manager + the OneNess TOR command brain.

The node *carries* Ollama: this module finds the binary, starts a server if none
is answering, installs Ollama when it is missing, lists/pulls/removes models and
runs the chat loop that lets the model call the live API.

The model is never handed a static fake schema: :func:`build_system_prompt`
injects :func:`core.api_catalog.prompt_text` (every endpoint and its return
shape) **plus a live snapshot** read from the Rust node at request time, and
every tool result is the real HTTP response (see :mod:`core.tools`).

Base URL resolution order:
1. ``OLLAMA_HOST`` (Ollama's own variable — ``host:port`` or a full URL),
2. ``ONENESS_OLLAMA_URL`` / ``ollama.base_url`` in ``config.yaml``,
3. ``http://127.0.0.1:11434``.

A bearer token from ``OLLAMA_API_KEY`` / ``ONENESS_OLLAMA_API_KEY`` is sent on
every call, so ``ONENESS_OLLAMA_URL=https://ollama.com`` runs the model on
Ollama's own hardware — the answer for a host that cannot hold one.
"""
from __future__ import annotations

import functools
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

from core import api_catalog
from core.config import settings
from core.errors import OllamaUnavailable, ValidationError
from core.nft import persona_block as nft_persona_block
from core.tools import ToolBox, summarize

DEFAULT_URL = "http://127.0.0.1:11434"
INSTALL_SH = "https://ollama.com/install.sh"
DOWNLOAD_URL = "https://ollama.com/download"
#: No-sudo fallback for Linux containers (Render, Docker) when install.sh fails.
LINUX_TARBALL = ("https://github.com/ollama/ollama/releases/latest/download/"
                 "ollama-linux-{arch}.tgz")

#: Hosts that mean "the machine this process is on". Anything else is somebody
#: else's Ollama: it is never installed, started or killed from here.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""}
#: Env vars that carry the bearer token a hosted Ollama (ollama.com, a tunnel)
#: needs. Checked in order; the value is never rendered, logged or returned.
API_KEY_ENVS = ("ONENESS_OLLAMA_API_KEY", "OLLAMA_API_KEY")

#: Ollama's own hosted API: the weights run there, so a machine that cannot hold
#: a model (a 512 MB instance) answers with just this URL plus a bearer token.
HOSTED_URL = "https://ollama.com"

#: Models to fall back to on a *hosted* Ollama when the configured local model
#: is not in that host's catalogue — tool-capable, small enough to answer fast.
REMOTE_PREFERRED = ("gpt-oss:20b", "gpt-oss:120b", "nemotron-3-nano:30b",
                    "gemma4:31b", "mistral-large-4")



# ---------------------------------------------------------------- location
def base_url() -> str:
    """The address of the Ollama API, honouring ``OLLAMA_HOST``."""
    raw = os.environ.get("OLLAMA_HOST", "").strip()
    if raw:
        if not raw.startswith(("http://", "https://")):
            raw = f"http://{raw}"
        host = raw.split("//", 1)[1].split(":")[0]
        if host in ("0.0.0.0", "::", "[::]", ""):
            return DEFAULT_URL
        return raw.rstrip("/")
    return str(settings.get("ollama.base_url") or DEFAULT_URL).rstrip("/")


def is_local(url: str | None = None) -> bool:
    """Is this Ollama *this* machine's process (so we may install/start it)?"""
    text = str(url or base_url()).strip()
    parts = urllib.parse.urlsplit(text if "://" in text else "http://" + text)
    return (parts.hostname or "").lower() in LOOPBACK_HOSTS


def api_key() -> str:
    """Bearer token for a hosted Ollama — the secret itself never leaves here.

    ``ONENESS_OLLAMA_URL=https://ollama.com`` plus this key is how a box that
    cannot hold a model (a 512 MB Render instance) still gets a real brain:
    ollama.com runs the weights and returns native tool calls over HTTPS.
    """
    for name in API_KEY_ENVS:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return str(settings.get("ollama.api_key") or "").strip()


def auth_error_hint() -> str:
    """What an operator must do about a 401/403 from the configured host."""
    if api_key():
        return ("the API key was rejected — check OLLAMA_API_KEY / "
                "ONENESS_OLLAMA_API_KEY")
    return ("this host needs an API key — set OLLAMA_API_KEY (or "
            "ONENESS_OLLAMA_API_KEY) for ollama.com or a secured tunnel")


def hosted_hint() -> str:
    """What a machine that cannot run a model can do instead — and it can.

    A small container has no ``ollama`` binary and no RAM for weights, so
    "install it" is a dead end there. The exit is the same two variables the
    README documents; the key itself is never echoed, only whether it is set.
    """
    token = api_key()
    return (f" — a machine that cannot run a model sets "
            f"ONENESS_OLLAMA_URL={HOSTED_URL} and lets Ollama's hardware answer "
            f"({'API key is set' if token else 'add ONENESS_OLLAMA_API_KEY too'})")


def unconfigured() -> bool:
    """Nobody chose an Ollama address: no ``OLLAMA_HOST``, no explicit URL.

    ``config.yaml`` ships ``ollama.base_url: http://127.0.0.1:11434``, so an
    untouched install *looks* configured while meaning "the default". Only this
    state may be changed by :func:`adopt_hosted` — a URL somebody typed, in the
    environment or in the YAML, is a decision and is never overridden.
    """
    if os.environ.get("OLLAMA_HOST", "").strip():
        return False
    if os.environ.get("ONENESS_OLLAMA_URL", "").strip():
        return False
    return base_url() == DEFAULT_URL


def adopt_hosted(*, auto_install: bool = False) -> str | None:
    """Use ollama.com when this box can neither run a model nor was given one.

    A 512 MB container has no ``ollama`` binary and no RAM for weights; when a
    token is already present and the operator never chose a URL, the honest exit
    is the documented one — set ``ONENESS_OLLAMA_URL=https://ollama.com`` *here*,
    so ``base_url()``, ``/api/health`` and the boot banner all tell one story
    instead of reporting "nothing answers at 127.0.0.1:11434" forever.

    Nothing that was chosen is ever overridden: an explicit ``OLLAMA_HOST`` /
    ``ONENESS_OLLAMA_URL``, a non-default ``ollama.base_url``, an installed
    binary, a loopback Ollama that does answer, or an operator who asked to
    install here (``auto_install``) all win over this. Returns the URL adopted,
    or ``None`` when the configuration was left alone.
    """
    if auto_install or not unconfigured() or not api_key():
        return None
    if is_installed() or discover_local():
        return None
    os.environ["ONENESS_OLLAMA_URL"] = HOSTED_URL
    return HOSTED_URL


#: Ports probed when the *configured* loopback address is silent but Ollama is
#: probably running on this machine anyway. Ollama's desktop app and a manual
#: ``OLLAMA_HOST=:8080 ollama serve`` both pin a port, so "nothing at 11434" does
#: not mean "nothing installed" — the message should say where it *is*.
DISCOVERY_PORTS = (11434, 8080, 11435, 11436)


def discover_local(timeout: float = 0.4) -> str | None:
    """A loopback Ollama that answers ``/api/version`` — never the one configured.

    Read-only and best-effort. Used only to keep the offline message honest
    ("something answers on 127.0.0.1:8080"): it never changes which URL is used,
    because silently talking to a different port than the operator configured is
    exactly the kind of lie this module refuses to tell.
    """
    if not is_local():
        return None
    current = base_url()
    for port in DISCOVERY_PORTS:
        url = f"http://127.0.0.1:{port}"
        if url == current:
            continue
        try:
            with urllib.request.urlopen(f"{url}/api/version", timeout=timeout) as reply:
                payload = json.loads(reply.read().decode("utf-8", "replace") or "{}")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("version"):
            return url
    return None


def port_hint() -> str:
    """``"; Ollama answers on …"`` when it runs on this machine at another port."""
    found = discover_local()
    if not found:
        return ""
    return (f" — an Ollama *is* answering on {found}: set ONENESS_OLLAMA_URL="
            f"{found} (or OLLAMA_HOST) and restart, instead of installing or "
            f"starting a second server")


def install_dir() -> Path:
    """Where the no-sudo Linux/Windows tarball install lands (``~/.local/ollama``)."""
    raw = os.environ.get("OLLAMA_INSTALL_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".local" / "ollama"


def find_binary() -> str | None:
    """Absolute path to the ``ollama`` executable, if it is installed."""
    override = os.environ.get("OLLAMA_BIN", "").strip()
    if override:
        try:
            if Path(override).exists():
                return override
        except OSError:
            pass
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [
        install_dir() / "bin" / "ollama",
        Path.home() / ".local" / "bin" / "ollama",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Ollama" / "ollama.exe",
        Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"),
        Path("/opt/homebrew/bin/ollama"),
        Path("/Applications/Ollama.app/Contents/Resources/ollama"),
    ]

    for path in candidates:
        try:
            if str(path) and path.exists():
                return str(path)
        except OSError:
            continue
    return None


def is_installed() -> bool:
    return find_binary() is not None


def version() -> str | None:
    """``ollama --version`` → ``'0.40.6'`` (no server needed)."""
    binary = find_binary()
    if not binary:
        return None
    try:
        result = subprocess.run([binary, "--version"], capture_output=True,
                                text=True, timeout=20, encoding="utf-8",
                                errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip()
    for token in text.replace("ollama version is", "").replace(
            "ollama version", "").split():
        if token[:1].isdigit():
            return token.strip()
    return text or None


# ------------------------------------------------------------------- HTTP
def _url(path: str) -> str:
    return f"{base_url()}{path if path.startswith('/') else '/' + path}"


def _request(path: str, payload: dict | None = None, *,
             method: str | None = None, timeout: float | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"accept": "application/json"}
    token = api_key()
    if token:
        headers["authorization"] = f"Bearer {token}"
    if data is not None:
        headers["content-type"] = "application/json"
    verb = method or ("POST" if data is not None else "GET")
    waited = float(timeout or settings.get("ollama.timeout"))
    request = urllib.request.Request(_url(path), data=data, headers=headers,
                                     method=verb)
    try:
        with urllib.request.urlopen(request, timeout=waited) as reply:
            body = reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        message = body
        try:
            message = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            pass
        if error.code in (401, 403):
            raise OllamaUnavailable(
                f"Ollama said HTTP {error.code}: {auth_error_hint()} "
                f"({str(message)[:200]})",
                details={"url": _url(path), "status": error.code,
                         "api_key": bool(token)}) from error
        raise OllamaUnavailable(f"Ollama said HTTP {error.code}: {message[:300]}",
                                details={"url": _url(path)}) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        text = str(reason).lower()
        if isinstance(reason, TimeoutError) or isinstance(error, TimeoutError) \
                or "timed out" in text or "timeout" in text:
            raise OllamaUnavailable(
                f"Ollama did not answer within {waited:g}s at {base_url()} — the model "
                f"may still be loading, or the prompt is large; raise `ollama.timeout` "
                f"or pick a faster model",
                details={"url": _url(path), "timeout_seconds": waited}) from error
        raise OllamaUnavailable(
            f"Ollama is not answering at {base_url()} ({reason}) — use the "
            f"install/start button or run `ollama serve`",
            details={"url": _url(path)}) from error
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as error:
        raise OllamaUnavailable(f"Ollama returned non-JSON: {body[:200]}") from error


def _stream(path: str, payload: dict, timeout: float = 3600.0) -> Iterator[dict]:
    """Yield NDJSON objects (pull progress, streamed generation)."""
    headers = {"content-type": "application/json",
               "accept": "application/x-ndjson"}
    token = api_key()
    if token:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        _url(path), data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        reply = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        if error.code in (401, 403):
            raise OllamaUnavailable(
                f"Ollama said HTTP {error.code}: {auth_error_hint()}") from error
        raise OllamaUnavailable(f"Ollama said HTTP {error.code}: {body[:300]}") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise OllamaUnavailable(f"Ollama is not answering at {base_url()} "
                                f"({getattr(error, 'reason', error)})") from error
    with reply:
        for line in reply:
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                yield {"status": text}


# -------------------------------------------------------------- lifecycle
def api_version() -> dict | None:
    try:
        return _request("/api/version", timeout=5)
    except OllamaUnavailable:
        return None


def is_serving() -> bool:
    return api_version() is not None


def models(limit: int | None = None) -> list[dict]:
    """Installed models, normalised (name, size, family, parameters)."""
    tags = _request("/api/tags").get("models", [])
    rows = []
    for item in tags:
        details = item.get("details") or {}
        rows.append({"name": item.get("name"), "model": item.get("model"),
                     "size": int(item.get("size") or 0),
                     "size_gb": round(int(item.get("size") or 0) / 1024 ** 3, 2),
                     "modified_at": item.get("modified_at"),
                     "digest": item.get("digest"),
                     "family": details.get("family"),
                     "parameters": details.get("parameter_size"),
                     "quantization": details.get("quantization_level"),
                     "cloud": ":cloud" in str(item.get("name", ""))})
    rows.sort(key=lambda row: (row["cloud"], row["name"] or ""))
    return rows[:limit] if limit else rows


def running() -> list[dict]:
    rows = []
    for item in _request("/api/ps").get("models", []):
        rows.append({"name": item.get("name"), "size": int(item.get("size") or 0),
                     "size_vram": int(item.get("size_vram") or 0),
                     "expires_at": item.get("expires_at")})
    return rows


def show(model: str) -> dict:
    return _request("/api/show", {"model": model}, timeout=30)


@functools.lru_cache(maxsize=128)
def supports_thinking(model: str) -> bool:
    """True when ``/api/show`` lists the ``thinking`` capability (cached).

    The builders ask for structured output, so a reasoning trace is pure cost:
    it can eat the whole ``num_predict`` budget before the plan starts. Only a
    model that really declares ``thinking`` gets ``think=False`` — sending the
    flag to a plain model is an HTTP 400 (``does not support thinking``).
    """
    name = str(model or "").strip()
    if not name:
        return False
    try:
        capabilities = show(name).get("capabilities") or []
    except (OllamaUnavailable, ValueError, AttributeError):
        return False
    return any(str(capability).strip().lower() == "thinking"
               for capability in capabilities)


def delete(model: str) -> dict:
    _request("/api/delete", {"model": model}, method="DELETE", timeout=60)
    return {"deleted": model}


def pull(model: str) -> Iterator[dict]:
    """Stream ``/api/pull`` progress objects."""
    if not model or "/" not in model and ":" not in model and len(model) < 2:
        raise ValidationError("a model name is required, e.g. llama3.2:3b")
    yield from _stream("/api/pull", {"model": model, "stream": True})


def pull_blocking(model: str) -> dict:
    last = {}
    for chunk in pull(model):
        last = chunk
        if chunk.get("error"):
            raise OllamaUnavailable(f"pull failed: {chunk['error']}")
    return {"model": model, "status": last.get("status", "success"),
            "completed": last.get("completed"), "total": last.get("total")}


def generate(model: str, prompt: str, *, system: str | None = None,
             options: dict | None = None,
             format: str | dict | None = None,
             think: bool | None = None) -> dict:
    """``/api/generate`` — ``format="json"`` asks Ollama for structured output.

    ``think=False`` switches the reasoning trace off. Thinking models
    (``deepseek*``, ``glm*``, ``gpt-oss*``, ``nemotron*`` …) otherwise spend the
    whole ``num_predict`` budget inside ``thinking`` and hand back an empty
    ``response`` with ``done_reason="length"`` — which looks like "the model
    ignored the instruction" but is really "the answer never started".
    Non-thinking models ignore the flag; ``think=True`` on them is an HTTP 400,
    so callers use :func:`supports_thinking` first.

    The reply keeps ``thinking`` and ``done_reason`` so a caller can tell a
    truncated answer from a refused one.
    """
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    if options:
        payload["options"] = options
    if format:
        payload["format"] = format
    if think is not None:
        payload["think"] = bool(think)
    reply = _request("/api/generate", payload)
    return {"model": reply.get("model", model),
            "response": reply.get("response", ""),
            "thinking": reply.get("thinking", ""),
            "done_reason": reply.get("done_reason"),
            "eval_count": reply.get("eval_count"),
            "total_duration": reply.get("total_duration")}


def run(model: str, prompt: str) -> dict:
    """The API behind the ``ollama run <model>`` box."""
    return generate(model, prompt)


def chat(messages: list[dict], model: str, *, tools: list[dict] | None = None,
         options: dict | None = None) -> dict:
    payload: dict = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if options:
        payload["options"] = options
    return _request("/api/chat", payload)


# ------------------------------------------------------------ install/start
def _serve_env() -> dict:
    """Env for ``ollama serve``: bind where ``base_url()`` says when unset."""
    env = dict(os.environ)
    if env.get("OLLAMA_HOST", "").strip():
        return env
    url = base_url()
    if "://" in url:
        host_port = url.split("//", 1)[1].rstrip("/")
        if host_port:
            env["OLLAMA_HOST"] = host_port
    return env


def start_server(wait: float = 30.0) -> dict:
    """Start ``ollama serve`` when nothing answers, then wait for /api/version."""
    if not is_local():
        raise OllamaUnavailable(
            f"{base_url()} is another machine's Ollama — there is no local "
            f"server to start here; unset ONENESS_OLLAMA_URL to use "
            f"{DEFAULT_URL}, or point it at the host that really runs it")
    if is_serving():
        return {"started": False, "already_running": True, "api_url": base_url(),
                "version": (api_version() or {}).get("version")}
    binary = find_binary()
    if not binary:
        # "install it" is not a fix on a box that cannot hold weights: prefer the
        # loopback port that *did* answer, otherwise name the hosted exit.
        raise OllamaUnavailable(
            f"no `ollama` binary on this machine and nothing answers at "
            f"{base_url()}" + (port_hint() or hosted_hint()),
            details={"api_url": base_url(), "api_key": bool(api_key()),
                     "install": "POST /api/ollama/install"})
    flags = 0
    if platform.system() == "Windows":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen([binary, "serve"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True, env=_serve_env())
    except OSError as error:
        raise OllamaUnavailable(f"cannot start `ollama serve`: {error}") from error
    deadline = time.time() + float(wait)
    while time.time() < deadline:
        if is_serving():
            return {"started": True, "already_running": False, "api_url": base_url(),
                    "version": (api_version() or {}).get("version")}
        time.sleep(0.8)
    raise OllamaUnavailable(
        f"`ollama serve` was launched but {base_url()} still does not answer — "
        f"check that OLLAMA_HOST ({os.environ.get('OLLAMA_HOST', 'unset')}) "
        f"points at a free port")


def _install_linux_tarball(timeout: float) -> tuple[bool, str]:
    """No-sudo fallback: the release tarball into ``install_dir()`` (Render/Docker)."""
    target = install_dir()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    url = LINUX_TARBALL.format(arch=arch)
    script = ("set -e; tmp=$(mktemp -d); "
              f"curl -fsSL '{url}' -o \"$tmp/ollama.tgz\"; "
              f"tar -xzf \"$tmp/ollama.tgz\" -C '{target}'; "
              "rm -rf \"$tmp\"")
    try:
        target.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["sh", "-c", script], capture_output=True,
                                text=True, timeout=float(timeout),
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"\n$ tarball {url} failed: {error}"
    log = (f"\n$ curl -fsSL {url} | tar -xz -C {target}\n"
           f"{(result.stdout or '')[-800:]}{(result.stderr or '')[-1500:]}")
    return (target / "bin" / "ollama").exists(), log


def install(timeout: float = 1200.0) -> dict:
    """Install Ollama (winget on Windows, install.sh elsewhere, tarball fallback)."""
    if not is_local():
        return {"installed": False, "already": False, "remote": True,
                "binary": None, "version": None,
                "log": (f"{base_url()} is another machine's Ollama — nothing is "
                        f"installed or downloaded here"),
                "next": ("a hosted Ollama is installed where it runs; set "
                         "OLLAMA_API_KEY when that host asks for one, e.g. "
                         "ONENESS_OLLAMA_URL=https://ollama.com")}
    if is_installed():
        return {"installed": True, "already": True, "binary": find_binary(),
                "version": version(), "log": "Ollama is already installed."}
    system = platform.system()
    commands: list[list[str]] = []
    log = ""
    if system == "Windows":
        winget = shutil.which("winget")
        if winget:
            commands.append([winget, "install", "--id", "Ollama.Ollama", "-e",
                             "--silent", "--accept-package-agreements",
                             "--accept-source-agreements"])
        commands.append(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-Command",
                         f"$p=Join-Path $env:TEMP 'OllamaSetup.exe'; "
                         f"Invoke-WebRequest -UseBasicParsing '{DOWNLOAD_URL}/OllamaSetup.exe' "
                         f"-OutFile $p; Start-Process -FilePath $p -ArgumentList '/S' -Wait"])
    elif system == "Darwin":
        commands.append(["sh", "-c",
                         f"curl -fsSL {INSTALL_SH} | sh || "
                         f"(curl -fsSL {DOWNLOAD_URL}/Ollama-darwin.zip -o /tmp/o.zip && "
                         f"unzip -o /tmp/o.zip -d /Applications)"])
    else:
        commands.append(["sh", "-c", f"curl -fsSL {INSTALL_SH} | sh"])
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=float(timeout), encoding="utf-8",
                                    errors="replace")
        except (OSError, subprocess.SubprocessError) as error:
            log += f"\n$ {' '.join(command[:3])}… failed: {error}"
            continue
        log += (f"\n$ {' '.join(command[:4])}…\n"
                f"{(result.stdout or '')[-1500:]}{(result.stderr or '')[-1500:]}")
        if is_installed():
            break
    if not is_installed() and system not in ("Windows", "Darwin"):
        installed, extra = _install_linux_tarball(timeout)
        log += extra
    installed = is_installed()
    return {"installed": installed, "already": False, "binary": find_binary(),
            "version": version() if installed else None,
            "log": log.strip() or "no installer output",
            "next": ("POST /api/ollama/start (or restart the start command) and "
                     "pull a model" if installed else "install manually from "
                     + DOWNLOAD_URL)}



def status() -> dict:
    """Install + API state, never raising (the dashboard polls this)."""
    binary = find_binary()
    remote, token = not is_local(), api_key()
    reachable, version_text, model_rows, running_rows = False, None, [], []
    error: str | None = None
    try:
        payload = _request("/api/version", timeout=5)
        failure = ""
    except OllamaUnavailable as problem:
        payload, failure = None, problem.message
    if payload is None:
        if remote:
            error = (f"{failure or 'nothing answered'} — ONENESS_OLLAMA_URL is "
                     f"{base_url()}, a hosted address, so this process neither "
                     f"installs nor starts it. Check that host or tunnel"
                     + ("" if token else ", and set OLLAMA_API_KEY (or "
                        "ONENESS_OLLAMA_API_KEY) when it asks for a token"))
        else:
            error = (f"Ollama is not answering at {base_url()} — install it or "
                     f"press \u201cstart server\u201d, then pull a model. A host that "
                     f"cannot hold a model (a 512 MB instance) sets "
                     f"ONENESS_OLLAMA_URL=https://ollama.com + OLLAMA_API_KEY and "
                     f"runs it there" + port_hint())
    else:
        version_text = payload.get("version")
        reachable = bool(version_text)
        try:
            model_rows = models()
        except OllamaUnavailable as problem:
            error = problem.message
        try:
            running_rows = running()
        except OllamaUnavailable:
            running_rows = []        # /api/ps is not exposed by every host
    return {"installed": binary is not None, "binary": binary,
            "api_url": base_url(), "reachable": reachable, "remote": remote,
            "api_key": bool(token),
            "version": version_text or version(), "error": error,
            "models": model_rows, "model_count": len(model_rows),
            "running": running_rows,
            "default_model": pick_model(model_rows=model_rows) if reachable else
            str(settings.ollama_model)}


# ------------------------------------------------------ model per AI surface
#: The four AI surfaces an operator can point at a different local model. The
#: admin page renders one dropdown per entry, fed by ``/api/ollama/models``.
PURPOSES: tuple[dict, ...] = (
    {"id": "brain", "label": "Blockchain interface (command deck)",
     "config": "ollama.models.brain",
     "summary": "TOR-MIND: the live API schema plus real fetch_oneness_data calls"},
    {"id": "nft", "label": "AI-NFT Studio",
     "config": "ollama.models.nft",
     "summary": "the minted token speaks as itself — persona prompt, no tool schema"},
    {"id": "web_builder", "label": "DarkWeb App Builder",
     "config": "ollama.models.web_builder",
     "summary": "multi-file web apps in /workbench — HTML, CSS, JS, Node"},
    {"id": "rust_builder", "label": "Rust Program Builder",
     "config": "ollama.models.rust_builder",
     "summary": "cargo projects in /workbench — Cargo.toml + src/*.rs"},
)
PURPOSE_IDS: tuple[str, ...] = tuple(spec["id"] for spec in PURPOSES)


def _routing_path() -> Path:
    """Where admin overrides live (data dir, so ``ONENESS_DATA_DIR`` moves it)."""
    return settings.data_dir / "ollama_routing.json"


def routing_overrides() -> dict[str, str]:
    """``{purpose: model}`` written by the admin page (empty when never set)."""
    try:
        payload = json.loads(_routing_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value).strip() for key, value in payload.items()
            if str(key) in PURPOSE_IDS and isinstance(value, str) and value.strip()}


def _write_routing(rows: dict[str, str]) -> None:
    path = _routing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(rows, indent=2, sort_keys=True),
                         encoding="utf-8")
    os.replace(temporary, path)


def configured_model(purpose: str | None = None) -> str:
    """Admin override → ``ollama.models.<purpose>`` → ``ollama.model``."""
    if purpose:
        override = routing_overrides().get(str(purpose))
        if override:
            return override
        per_surface = settings.get(f"ollama.models.{purpose}")
        if per_surface not in (None, ""):
            return str(per_surface).strip()
    return str(settings.get("ollama.model") or "").strip()


def model_for(purpose: str) -> str:
    """The model that actually answers for ``purpose`` right now."""
    return pick_model(purpose=purpose)


def routing(model_rows: list[dict] | None = None) -> dict:
    """Every AI surface with the model it uses, whether that model is on disk."""
    rows, error = model_rows, None
    if rows is None:
        try:
            rows = models()
        except OllamaUnavailable as problem:  # honest: nothing installed is claimed
            rows, error = [], problem.message
    overrides = routing_overrides()
    available = {str(row.get("name")) for row in rows if row.get("name")}
    purposes = []
    for spec in PURPOSES:
        chosen = configured_model(spec["id"])
        if overrides.get(spec["id"]):
            source = "admin override"
        elif settings.get(spec["config"]) not in (None, ""):
            source = "config.yaml"
        else:
            source = "ollama.model"
        purposes.append({
            "id": spec["id"], "label": spec["label"], "summary": spec["summary"],
            "config": spec["config"], "model": chosen, "source": source,
            "installed": chosen in available,
            "effective": pick_model(model_rows=rows, purpose=spec["id"])})
    return {"purposes": purposes, "models": rows, "model_count": len(rows),
            "default_model": pick_model(model_rows=rows), "api_url": base_url(),
            "override_file": str(_routing_path()), "error": error}


def set_routing(purpose: str, model: str | None) -> dict:
    """Point one surface at an installed model (or clear the override).

    The model must exist on this device — a dropdown that saves a typo would be
    a lie the next generation call would pay for.
    """
    key = str(purpose or "").strip().lower()
    if key not in PURPOSE_IDS:
        raise ValidationError(
            f"unknown AI surface {purpose!r}",
            details={"known": list(PURPOSE_IDS)})
    overrides = routing_overrides()
    text = str(model or "").strip()
    if not text:
        overrides.pop(key, None)
        _write_routing(overrides)
        return {**routing(), "changed": key, "model": configured_model(key),
                "cleared": True}
    target = _clean_model(text)
    try:
        rows = models()
    except OllamaUnavailable as problem:
        raise ValidationError(
            f"cannot verify {target!r} — Ollama is not answering at {base_url()} "
            f"({problem.message}); start it from /ollama first") from problem
    available = {str(row.get("name")) for row in rows if row.get("name")}
    if target not in available:
        raise ValidationError(
            f"{target!r} is not installed on this device",
            details={"installed": sorted(available),
                     "fix": f"POST /api/ollama/pull {{\"model\": \"{target}\"}}"})
    overrides[key] = target
    _write_routing(overrides)
    return {**routing(model_rows=rows), "changed": key, "model": target,
            "cleared": False}


PREFERRED_FAMILIES = ("qwen", "llama3", "llama4", "mistral", "mixtral",
                      "command-r", "firefunction", "gpt-oss", "deepseek",
                      "gemma3", "phi4", "granite")


def pick_model(preferred: str | None = None, *,
               model_rows: list[dict] | None = None,
               purpose: str | None = None) -> str:
    """Prefer the requested model, then the surface's model, then a local one."""
    configured = configured_model(purpose)
    if preferred:
        return str(preferred).strip()
    rows = model_rows
    if rows is None:
        try:
            rows = models()
        except OllamaUnavailable:
            return configured
    available = [row["name"] for row in rows if row.get("name")]
    if configured in available:
        return configured
    if not is_local():
        for name in REMOTE_PREFERRED:
            if name in available:
                return name
    local = [name for name in available
             if ":cloud" not in name and "embed" not in name.lower()]
    for family in PREFERRED_FAMILIES:
        for name in local:
            if family in name.lower():
                return name
    return local[0] if local else (available[0] if available else configured)


# ------------------------------------------------------- suggested models
#: The download buttons on ``/ollama``. ``size_gb`` is the approximate download
#: size so the operator knows what they are agreeing to before clicking.
SUGGESTED_MODELS: tuple[dict, ...] = (
    {"model": "qwen2.5:7b-instruct", "label": "command brain", "size_gb": 4.7,
     "tools": True,
     "note": "best native tool calling — the default for the command deck"},
    {"model": "qwen2.5:3b-instruct", "label": "balanced", "size_gb": 1.9,
     "tools": True, "note": "fits an 8 GB machine"},
    {"model": "llama3.2:3b", "label": "fast", "size_gb": 2.0, "tools": True,
     "note": "quick native tool calls and it stays in character"},
    {"model": "llama3.2:1b", "label": "tiny", "size_gb": 1.3, "tools": False,
     "note": "falls back to the JSON tool protocol"},
    {"model": "gemma2:2b", "label": "writer", "size_gb": 1.6, "tools": False,
     "note": "good copy for landing pages and long answers"},
    {"model": "nomic-embed-text", "label": "embeddings", "size_gb": 0.3,
     "tools": False, "note": "search and similarity work"},
)


def suggested(model_rows: list[dict] | None = None) -> list[dict]:
    """Curated models plus whether each one is already on disk.

    When Ollama is down the rows still render — ``installed`` is simply False,
    because claiming an unverified download would be a lie.
    """
    rows = model_rows
    if rows is None:
        try:
            rows = models()
        except OllamaUnavailable:
            rows = []
    installed = {str(row.get("name") or "") for row in rows}
    return [{**item, "installed": item["model"] in installed,
             "run": f"ollama run {item['model']}"} for item in SUGGESTED_MODELS]


# ------------------------------------------------------- one-click runtime
def bootstrap(*, pull: bool = True, model: str | None = None) -> dict:
    """Install → start → pull the first model: the whole chain in one call.

    Each step reports its real outcome, so a failed download is never hidden
    behind a green button.
    """
    steps: list[dict] = []
    install_step = install()
    steps.append({"step": "install", "ok": bool(install_step.get("installed")),
                  "already": bool(install_step.get("already")),
                  "version": install_step.get("version"),
                  "log": install_step.get("log")})
    result: dict = {"installed": bool(install_step.get("installed")),
                    "serving": False, "api_url": base_url(),
                    "version": install_step.get("version"), "models": [],
                    "pulled": None, "steps": steps}
    if not result["installed"]:
        result["next"] = (install_step.get("next")
                          or f"install Ollama from {DOWNLOAD_URL}")
        return result
    try:
        started = start_server()
    except OllamaUnavailable as problem:
        steps.append({"step": "start", "ok": False, "error": problem.message})
        result["next"] = problem.message
        return result
    steps.append({"step": "start", "ok": True,
                  "already_running": started.get("already_running"),
                  "api_url": started.get("api_url"),
                  "version": started.get("version")})
    result.update(serving=True, api_url=started.get("api_url"),
                  version=started.get("version") or result["version"])

    def _rows() -> list[dict]:
        try:
            return models()
        except OllamaUnavailable as problem:
            steps.append({"step": "models", "ok": False, "error": problem.message})
            return []

    rows = _rows()
    result["models"] = rows
    target = str(model or settings.get("ollama.model") or "").strip()
    if pull and target and not any(str(row.get("name")) == target for row in rows):
        try:
            pulled = pull_blocking(target)
        except OllamaUnavailable as problem:
            steps.append({"step": "pull", "ok": False, "model": target,
                          "error": problem.message})
            result["next"] = (f"Ollama is installed and serving, but {target} did "
                              f"not download: {problem.message}")
        else:
            steps.append({"step": "pull", "ok": True, **pulled})
            result["pulled"] = pulled
            rows = _rows()
            result["models"] = rows
    result["default_model"] = pick_model(model_rows=rows)
    return result


def ensure_runtime(wait: float = 20.0, *, auto_install: bool = False) -> dict:
    """Called by ``python -m oneness_tor``: the node carries Ollama.

    Starts ``ollama serve`` when the binary exists but nothing answers, and only
    installs when the operator opted in with ``ollama.auto_install: true`` in
    ``config.yaml``. Every outcome is returned instead of raised so the startup
    banner can print the truth; nothing is ever claimed that did not happen.

    A *hosted* ``ollama.base_url`` (ollama.com, a tunnel) is never installed or
    started from here: that machine owns its process, and this one only needs the
    URL — plus ``OLLAMA_API_KEY`` when the host asks for a token.

    A box that *cannot* run a model at all (no binary, nothing on loopback, no
    URL ever chosen) but already holds a token adopts the hosted exit instead of
    repeating an "install it" instruction it can never follow — see
    :func:`adopt_hosted`, which reports what it did in ``adopted_url``.
    """
    adopted = adopt_hosted(auto_install=auto_install)
    local = is_local()
    if is_serving():
        state = {"serving": True, "installed": is_installed(), "started": False,
                 "remote": not local, "api_url": base_url(),
                 "version": (api_version() or {}).get("version")}
        if adopted:
            state["adopted_url"] = adopted
        return state
    if not local:
        state = {"serving": False, "installed": False, "started": False,
                 "remote": True, "api_url": base_url(),
                 "hint": ("nothing to install or start: that Ollama runs on "
                          "another machine"),
                 "error": (f"no Ollama is answering at {base_url()} — it is a "
                           f"hosted address, so this process cannot install or "
                           f"start it. Check that host, and set OLLAMA_API_KEY "
                           f"when it asks for a token")}
        if adopted:
            state["adopted_url"] = adopted
        return state
    if not is_installed():
        if not auto_install:
            return {"serving": False, "installed": False, "started": False,
                    "api_url": base_url(),
                    "hint": "Ollama is not installed — POST /api/ollama/bootstrap",
                    "error": ("Ollama is not installed and auto-install is off "
                              "(ollama.auto_install / "
                              "ONENESS_OLLAMA_AUTO_INSTALL=0) — press bootstrap at "
                              "/ollama to install it"
                              + (port_hint() or hosted_hint()))}
        installed = install()
        if not installed.get("installed"):
            return {"serving": False, "installed": False, "started": False,
                    "api_url": base_url(), "install": installed,
                    "error": installed.get("next") or "the installer failed"}
    try:
        return {"serving": True, "installed": True, **start_server(wait=wait)}
    except OllamaUnavailable as problem:
        return {"serving": False, "installed": True, "started": False,
                "api_url": base_url(), "error": problem.message + port_hint()}


# ------------------------------------------------------------- ollama "cli"
CLI_VERBS = {"run": "run", "pull": "pull", "list": "list", "ls": "list",
             "ps": "ps", "serve": "serve", "start": "serve",
             "rm": "rm", "remove": "rm", "delete": "rm",
             "show": "show", "help": "help"}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]{0,160}$")
CLI_HELP = [
    "ollama run <model> [prompt]",
    "ollama pull <model>",
    "ollama list",
    "ollama ps",
    "ollama show <model>",
    "ollama rm <model>",
    "ollama serve",
]


def _clean_model(value: str) -> str:
    """A model name, validated before it ever reaches the Ollama API."""
    model = str(value or "").strip().strip('"').strip("'")
    if not model:
        raise ValidationError("a model name is required, e.g. llama3.2:3b",
                              details={"examples": CLI_HELP})
    if not MODEL_RE.match(model):
        raise ValidationError(
            f"{model!r} is not a model name Ollama accepts (letters, digits, "
            "'.', '_', ':', '/', '-')", details={"model": model})
    return model


def parse_command(text: str) -> dict:
    """Parse a typed ``ollama …`` line without executing anything."""
    raw = str(text or "").strip()
    if not raw:
        raise ValidationError(
            "a command is required, e.g. `ollama run llama3.2:3b hello`",
            details={"examples": CLI_HELP})
    parts = raw.split(None, 1)
    head = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if head == "ollama":
        words = rest.split(None, 1)
        head = words[0].lower() if words else ""
        rest = words[1].strip() if len(words) > 1 else ""
    verb = CLI_VERBS.get(head)
    if not verb:
        raise ValidationError(
            f"unknown ollama command {head or raw!r}",
            details={"known": sorted(set(CLI_VERBS.values())),
                     "examples": CLI_HELP})
    model, prompt = "", ""
    if verb in ("run", "pull", "rm", "show"):
        chunk = rest.split(None, 1)
        model = chunk[0] if chunk else ""
        prompt = chunk[1].strip() if len(chunk) > 1 else ""
        if model:
            model = _clean_model(model)
    return {"verb": verb, "model": model, "prompt": prompt, "raw": raw}


def run_cli(text: str, prompt: str | None = None) -> dict:
    """Execute a typed ``ollama …`` line against the API the node carries.

    ``run`` really generates, ``list``/``ps``/``show`` really read the runtime,
    ``rm`` really deletes, and ``pull`` hands back the streaming endpoint so the
    page can show the true download progress — nothing pretends work happened.
    """
    parsed = parse_command(text)
    verb, model = parsed["verb"], parsed["model"]
    if verb == "help":
        return {"kind": "help", "parsed": parsed, "commands": CLI_HELP}
    if verb == "run":
        target = model or pick_model()
        said = (prompt or parsed["prompt"] or "").strip()
        if not said:
            raise ValidationError(
                "`ollama run <model>` needs a prompt — type it after the model "
                "or in the prompt box",
                details={"example": "ollama run llama3.2:3b summarise the chain"})
        reply = generate(target, said)
        return {"kind": "run", "parsed": parsed, "model": reply["model"],
                "prompt": said, "response": reply["response"],
                "done_reason": reply["done_reason"],
                "eval_count": reply["eval_count"]}
    if verb == "pull":
        target = _clean_model(model or str(settings.get("ollama.model") or ""))
        return {"kind": "pull", "parsed": parsed, "model": target,
                "stream": {"endpoint": "/api/ollama/pull", "method": "POST",
                           "body": {"model": target}},
                "note": "call the streaming endpoint to watch real progress"}
    if verb == "list":
        rows = models()
        return {"kind": "list", "parsed": parsed, "models": rows,
                "count": len(rows)}
    if verb == "ps":
        rows = running()
        return {"kind": "ps", "parsed": parsed, "models": rows,
                "count": len(rows)}
    if verb == "show":
        target = _clean_model(model)
        return {"kind": "show", "parsed": parsed, "model": target,
                "info": show(target)}
    if verb == "rm":
        target = _clean_model(model)
        return {"kind": "rm", "parsed": parsed, "model": target, **delete(target)}
    if verb == "serve":
        return {"kind": "serve", "parsed": parsed, **start_server()}
    raise ValidationError(f"unsupported ollama command {verb!r}")


# --------------------------------------------------------- system prompt
PERSONA = (
    "You are TOR-MIND, the command brain of a OneNess TOR node. You are talking "
    "to the node operator. You can read the live blockchain, drive the wallet, "
    "sell and verify paid services (contracts 5 TOR, AI-NFT mint 3 TOR, meme "
    "launch 5 TOR), and report the real result of every "
    "action. You are honest about failures and you never invent data."
)

RULES = """RULES
1. Any fact about balances, height, supply, records, tokens, pools, prices,
   contracts or models MUST come from a fetch_oneness_data result in this
   conversation. If you have not fetched it, fetch it — never answer from memory.
2. When a call fails, quote the exact error code and message and say what the
   operator must fix. Never pretend a failed call succeeded.
3. Paid actions only happen through the API. If you are not allowed to call one
   (no admin token), show the exact endpoint + params the operator must run.
4. The chain stores microTOR (1 TOR = 100000000 microTOR). Show both when it
   matters, and format TOR with up to 8 decimals, trailing zeros trimmed.
5. Nothing on this node expires: every record the ledger holds stays public, and
   only the sending address of an *anonymous* transfer is masked (the receiver is
   never masked). Point at `/api/privacy` for the exact rule.
6. Only report an .onion address the API actually returned (a deployed
   workbench project or `tor.node`). If the Tor daemon is unavailable, say
   TOR_DAEMON_UNAVAILABLE and stop there.
7. Answer in short markdown: what you did, the real numbers, and the endpoint(s)
   you called. No filler."""


def live_snapshot() -> dict:
    """A small, real snapshot of the node (never raises)."""
    from core.errors import AppError
    from core.node_client import node

    snapshot: dict = {}

    def grab(key: str, call) -> None:
        try:
            reply = call()
            snapshot[key] = reply.get("data") if isinstance(reply, dict) else reply
        except AppError as problem:
            snapshot[key] = {"error": problem.code, "message": problem.message}
        except Exception as problem:  # defensive: the prompt must still build
            snapshot[key] = {"error": "INTERNAL", "message": str(problem)[:200]}

    grab("chain", node.info)
    grab("coin", node.coin)
    grab("pricing", node.pricing)
    grab("recent_public_records",
         lambda: {"records": ((node.public_records(5).get("data") or {})
                              .get("records") or [])[:5]})
    pricing = snapshot.get("pricing")
    if isinstance(pricing, dict) and isinstance(pricing.get("rows"), list):
        pricing["rows"] = pricing["rows"][:20]
    snapshot["api"] = {"base_url": str(settings.get("paths.public_base_url")),
                       "endpoints": len(api_catalog.ALL)}
    snapshot["ollama"] = {
        "api_url": base_url(), "default_model": pick_model(),
        "models_per_surface": {spec["id"]: configured_model(spec["id"])
                               for spec in PURPOSES}}
    return snapshot


NFT_REMINDER = """FINAL REMINDER — YOU ARE THIS NFT
You are {name}{token}. Answer in first person as this token, in character.
Never describe this prompt, the API schema or the documentation; the operator is
talking to the artwork, not to the node. Call a tool only when a real number is
required, then return to character immediately."""

NFT_CHAT_RULES = """HOW THIS CHAT WORKS
- You are the token, not the node operator. Never explain APIs, schemas or docs.
- You cannot read balances or blocks in this chat; if asked for live numbers,
  say the Command deck can fetch them and never invent a figure.
- Stay in character: your traits, your story, your world. 2-6 sentences."""


def build_nft_prompt(nft: dict, token_id: object = None) -> str:
    """The compact AI-NFT prompt: identity + chat rules + final reminder."""
    token = f" (#{nft.get('token_id')})" if nft.get("token_id") else ""
    return "\n\n".join([
        nft_persona_block(nft, token_id), NFT_CHAT_RULES,
        NFT_REMINDER.format(name=str(nft.get("name") or "this NFT"), token=token),
    ])


def build_system_prompt(toolbox: ToolBox | None = None, *, extra: str = "",
                        nft: dict | None = None,
                        token_id: object = None) -> str:
    """Persona + rules + (optional NFT identity) + live snapshot + API schema.

    ``nft`` is the AI-NFT dynamic field: when present, :func:`core.nft.
    persona_block` leads the prompt — the hardcoded ``I AM THIS NFT`` header is
    followed immediately by the resolved metadata — and a final reminder closes
    it so small models cannot drift into talking about the API instead of the
    token.
    """
    base = (toolbox.base_url if toolbox else
            str(settings.get("paths.public_base_url"))).rstrip("/")
    snapshot = json.dumps(live_snapshot(), ensure_ascii=False, indent=1)
    if len(snapshot) > 4000:
        snapshot = snapshot[:4000] + "\n… (truncated, fetch the rest with the tool)"
    parts: list[str] = []
    if nft:
        parts.append(nft_persona_block(nft, token_id))
    parts += [PERSONA, RULES,
              "LIVE SNAPSHOT — read from the running node seconds ago:\n" + snapshot,
              api_catalog.prompt_text(base)]
    if nft:
        token = (f" (#{nft.get('token_id')})"
                 if nft.get("token_id") else "")
        parts.append(NFT_REMINDER.format(
            name=str(nft.get("name") or "this NFT"), token=token))
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


# ------------------------------------------------------------- chat loop
TOOL_UNSUPPORTED: set[str] = set()

TOOL_PROTOCOL = """TOOL PROTOCOL (this model has no native tool calling)
To read anything from the node, reply with ONE JSON object and nothing else:
{"endpoint": "<one of the endpoint names above>", "params": {"<name>": "<value>"}}
The node executes it and sends you back the real JSON. Then answer using only
those values. Never invent a number and never answer a data question from memory."""


def _chat_for(model: str, messages: list[dict], tools: list[dict] | None) -> dict:
    """Chat with native tools when supported, else the JSON protocol."""
    if tools and model not in TOOL_UNSUPPORTED:
        try:
            return chat(messages, model, tools=tools)
        except OllamaUnavailable as problem:
            if "does not support tools" not in problem.message.lower():
                raise
            TOOL_UNSUPPORTED.add(model)
    return chat(messages, model)


def _call_parts(call: dict) -> tuple[str, dict]:
    """Extract (name, arguments) from the several tool-call shapes models emit."""
    function = call.get("function") if isinstance(call, dict) else None
    if isinstance(function, dict):
        name = str(function.get("name") or call.get("name") or "")
        arguments = function.get("arguments", call.get("arguments") or {})
    else:
        name = str(call.get("name") or "")
        arguments = call.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    return name, arguments if isinstance(arguments, dict) else {}


def _loose_params(text: str) -> dict:
    """Parse the sloppy JSON small models emit (unquoted keys, trailing commas)."""
    cleaned = re.sub(r"([{,]\s*)([A-Za-z_][\w-]*)\s*:", r'\1"\2":', text)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    cleaned = re.sub(r"'([^'\\]*)'", r'"\1"', cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _pseudo_calls(text: str) -> list[dict]:
    """Models without native tool support: read a JSON call out of the reply.

    Tolerant on purpose: tiny models write ``{endpoint: chain.info, params: {…}}``
    with unquoted keys or trailing commas, and a call with empty params is still
    far better than letting the model invent the numbers.
    """
    calls: list[dict] = []
    for chunk in _balanced_objects(text or ""):
        match = (re.search(r'"endpoint"\s*:\s*"([^"]+)"', chunk)
                 or re.search(r"endpoint\s*:\s*\"?([A-Za-z][\w.]*)", chunk))
        if not match:
            continue
        endpoint = match.group(1).strip().strip('"')
        params: dict = {}
        params_at = chunk.find('"params"')
        if params_at == -1:
            params_at = chunk.find("params")
        if params_at != -1:
            brace = chunk.find("{", params_at)
            if brace != -1:
                inner = _matching_object(chunk, brace)
                params = _loose_params(inner)
        calls.append({"function": {"name": "fetch_oneness_data",
                                   "arguments": {"endpoint": endpoint,
                                                 "params": params}}})
    return calls


def _matching_object(text: str, start: int) -> str:
    """The balanced ``{...}`` slice starting at ``start`` (or the rest)."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text[start:]


def _balanced_objects(text: str) -> list[str]:
    """Every top-level ``{...}`` block in the text."""
    blocks, depth, start = [], 0, None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start:index + 1])
                start = None
    return blocks


def run_command(prompt: str | None = None, *, model: str | None = None,
                messages: list[dict] | None = None, toolbox: ToolBox | None = None,
                use_tools: bool = True, max_rounds: int | None = None,
                system: str | None = None, nft: dict | None = None,
                token_id: object = None, nft_only: bool = False,
                purpose: str | None = None) -> dict:
    """One command: ask the model, execute its API calls, answer with real data.

    ``nft`` loads an AI-NFT identity into the system prompt (right after the
    hardcoded ``I AM THIS NFT`` header) so the same tool loop speaks as the token.
    ``nft_only`` gives the compact persona prompt with no tools — the AI-NFT
    chatbox uses it so a small model stays in character instead of reading docs.
    ``purpose`` selects the per-surface model from :func:`routing` (``brain`` or
    ``nft`` unless the caller overrides it).
    """
    if not prompt and not messages:
        raise ValidationError("prompt or messages is required")
    box = toolbox or ToolBox()
    chosen = pick_model(model, purpose=purpose or
                        ("nft" if nft and nft_only else "brain"))
    rounds = int(max_rounds if max_rounds is not None
                 else settings.get("ollama.max_tool_rounds") or 4)
    if nft and nft_only:
        instructions = build_nft_prompt(nft, token_id)
        use_tools = False
    else:
        instructions = (system or build_system_prompt(
            box, nft=nft, token_id=token_id))
        if system and nft:
            instructions += "\n\n" + nft_persona_block(nft, token_id)
    convo: list[dict] = [{"role": "system", "content": instructions}]
    if use_tools:
        convo[0]["content"] += "\n\n" + TOOL_PROTOCOL
    convo.extend(messages or [])
    if prompt:
        convo.append({"role": "user", "content": prompt})
    tool_calls: list[dict] = []
    api_calls: list[dict] = []
    answer, content = "", ""
    nudged = False
    for round_index in range(rounds + 1):
        allow_tools = use_tools and round_index < rounds
        reply = _chat_for(chosen, convo, [box.spec] if allow_tools else None)
        message = reply.get("message") or {}
        content = str(message.get("content") or "")
        calls = message.get("tool_calls") or []
        if not calls and allow_tools:
            calls = _pseudo_calls(content)
        assistant: dict = {"role": "assistant", "content": content}
        if message.get("tool_calls"):
            assistant["tool_calls"] = message["tool_calls"]
        convo.append(assistant)
        if not calls:
            # a model that ignored the tool on its first turn gets one reminder
            if allow_tools and not nudged and not tool_calls:
                nudged = True
                convo.append({"role": "user", "content": (
                    "Do not answer from memory. Call the API for this question: "
                    f"{prompt or ''}\nReply with the JSON tool call only, e.g. "
                    '{"endpoint": "chain.info", "params": {}}')})
                continue
            answer = content
            break
        for call in calls:
            name, arguments = _call_parts(call)
            endpoint = str(arguments.get("endpoint") or arguments.get("name") or "")
            params = arguments.get("params")
            if not isinstance(params, dict):
                params = {key: value for key, value in arguments.items()
                          if key not in ("endpoint", "name", "params")}
            if name != "fetch_oneness_data":
                result = {"ok": False, "endpoint": endpoint or name,
                          "http_status": 0, "latency_ms": 0, "data": None,
                          "error": {"code": "UNKNOWN_TOOL",
                                    "message": "only fetch_oneness_data exists, "
                                               f"not {name!r}"}}
            elif not endpoint:
                result = {"ok": False, "endpoint": "", "http_status": 0,
                          "latency_ms": 0, "data": None,
                          "error": {"code": "VALIDATION_ERROR",
                                    "message": "endpoint is required"}}
            else:
                try:
                    result = box.execute(endpoint, params)
                except ValidationError as problem:
                    result = {"ok": False, "endpoint": endpoint, "http_status": 400,
                              "latency_ms": 0, "data": None,
                              "error": problem.to_error()}
            tool_calls.append({"round": round_index, "name": name,
                               "endpoint": result.get("endpoint"),
                               "arguments": params})
            api_calls.append({"round": round_index,
                              "endpoint": result.get("endpoint"),
                              "request": result.get("request"),
                              "http_status": result.get("http_status"),
                              "latency_ms": result.get("latency_ms"),
                              "ok": result.get("ok")})
            payload = summarize(result)
            if chosen in TOOL_UNSUPPORTED:
                convo.append({"role": "user", "content": (
                    f"TOOL RESULT for {result.get('endpoint')}:\n{payload}\n"
                    f"Now write a short human-readable answer (2-4 sentences, no JSON, "
                    f"no code fences, numbers in TOR).")})
            else:
                convo.append({"role": "tool", "tool_name": "fetch_oneness_data",
                              "content": payload})
    else:
        answer = content
    return {"answer": answer, "model": chosen, "tool_calls": tool_calls,
            "api_calls": api_calls, "rounds": len(api_calls),
            "messages": convo, "use_tools": use_tools,
            "verified": bool(api_calls),
            "native_tools": chosen not in TOOL_UNSUPPORTED}

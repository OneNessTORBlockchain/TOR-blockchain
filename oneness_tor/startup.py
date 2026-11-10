"""The one start command — the API, the Rust ledger and Ollama, in any host.

    python scripts/start.py            # same command locally, on Render, in Docker
    python -m oneness_tor              # identical code path
    python scripts/start.py --port 9000 --ollama-pull

Bind resolution: ``--host/--port`` → ``$PORT`` (Render) → ``ONENESS_API_HOST`` /
``ONENESS_API_PORT`` → ``api.host`` / ``api.port`` → ``127.0.0.1:8090``. When a
platform injects ``$PORT`` the default becomes ``0.0.0.0``, otherwise the health
check never reaches the container.

Ollama is *carried* by the node: when the binary is missing it is installed
(``ollama.auto_install``), then ``ollama serve`` is started and waited for. The
default model is pulled only when asked (``ollama.pull_on_start`` /
``ONENESS_OLLAMA_PULL=1``) — a 7B download is several GB and must not surprise a
deploy. Every outcome is printed as it really happened; nothing is faked.

The Rust ledger is *hosted* here as well when ``node.api_url`` is loopback
(``node.autostart``; ``--no-node`` opts out, ``node.api_url`` pointing at another
host is never touched). One container therefore runs the API **and**
``torchain serve``, and a fresh container adopts a verified snapshot
(``ONENESS_NODE_SEED`` or ``seed/chain-snapshot.json``) before the node starts —
see :mod:`core.ledger_host`.
"""
from __future__ import annotations

import argparse
import os
import signal
import threading

from core import ledger_host, ollama
from core.config import settings
from core.errors import AppError, OllamaUnavailable
from core.node_client import node
from oneness_tor.app import create_app
from oneness_tor.services import (chain_token, node_sync, privacy,
                                  token_media, tor_service)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


# ------------------------------------------------------------------- address
def resolve_bind(host: str | None = None, port: object = None) -> tuple[str, int]:
    """The interface/port to listen on, in the documented order."""
    raw_port = port or os.environ.get("PORT") or settings.get("api.port") or DEFAULT_PORT
    try:
        resolved_port = int(raw_port)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        resolved_port = DEFAULT_PORT
    raw_host = (host or os.environ.get("ONENESS_API_HOST")
                or settings.get("api.host") or DEFAULT_HOST)
    resolved_host = str(raw_host).strip() or DEFAULT_HOST
    if os.environ.get("PORT") and not host and not os.environ.get("ONENESS_API_HOST"):
        resolved_host = "0.0.0.0"               # a platform proxy needs every interface
    return resolved_host, resolved_port


# -------------------------------------------------------------------- ollama
def ollama_runtime(*, auto_install: bool, pull: bool, wait: float) -> dict:
    """Install + serve Ollama when needed, and pull the default model on request."""
    runtime = ollama.ensure_runtime(wait=wait, auto_install=auto_install)
    runtime["pulled"] = None
    if not runtime.get("serving") or not pull:
        return runtime
    target = str(settings.get("ollama.model") or "").strip()
    if not target:
        return runtime
    try:
        rows = ollama.models()
    except OllamaUnavailable:
        rows = []
    if any(str(row.get("name")) == target for row in rows):
        return runtime
    try:
        runtime["pulled"] = ollama.pull_blocking(target)
    except OllamaUnavailable as problem:
        runtime["pull_error"] = problem.message
    return runtime


# --------------------------------------------------------------------- serve
def serve(app, host: str, port: int, *, debug: bool = False) -> None:
    """A threaded WSGI server that shuts down cleanly on SIGTERM (Render, Docker)."""
    if debug:
        app.run(host=host, port=port, debug=True, threaded=True)
        return
    from werkzeug.serving import make_server

    server = make_server(host, port, app, threaded=True)

    def _shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        handle = getattr(signal, name, None)
        if handle is None:
            continue
        try:
            signal.signal(handle, _shutdown)
        except (ValueError, OSError):      # not the main thread / no such signal
            pass
    print(f"  serving        -> http://{host}:{port}  (threaded WSGI)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ledger_host.stop()


def banner(base: str, ledger: dict | None = None) -> None:
    """Everything an operator needs to know, printed from the real objects."""
    ledger = ledger or {}
    reachable = (bool(ledger.get("reachable")) if ledger
                 else node.is_reachable())
    print(f"OneNess TOR API  -> {base}", flush=True)
    print(f"  pages          -> {base}/  (dashboard: {base}/dashboard)", flush=True)
    print(f"  docs           -> {base}/docs   catalog: {base}/api/docs", flush=True)
    pinned = os.environ.get("ONENESS_PUBLIC_BASE_URL", "").strip()
    if pinned:
        print(f"  public base    -> {pinned}  "
              f"(ONENESS_PUBLIC_BASE_URL wins in /api/docs)", flush=True)
    if ledger.get("started"):
        state = (f"started by this command (pid {ledger.get('pid')}, "
                 f"{ledger.get('chain_dir')})"
                 if ledger.get("reachable")
                 else f"STARTED BUT NOT ANSWERING — {ledger.get('hint')}")
    elif reachable:
        state = "reachable (a node was already running — not touched)"
    else:
        state = "DOWN — no local ledger is running"
    print(f"  Rust node      -> {node.base_url} ({state})", flush=True)
    if ledger.get("seed"):
        seed = ledger["seed"]
        print(f"  chain seed     -> adopted height {seed.get('height')} "
              f"({seed.get('tokens')} tokens, head {str(seed.get('head_hash'))[:16]}…) "
              f"from {seed.get('source')}", flush=True)
    elif ledger and not reachable and ledger.get("hint"):
        print(f"                   {ledger['hint']}", flush=True)
    if ledger and not reachable and ledger.get("error"):
        print(f"                   node error: {ledger['error']}", flush=True)
    print(f"  privacy        -> asked per transaction: public puts the sender's "
          f"address on chain, anonymous shows {privacy.label()}", flush=True)
    print(f"  public data    -> tokens + totals + balances, readable forever "
          f"({base}/api/public/ledger)", flush=True)
    logos = token_media.status()
    mirrored = (logos.get("logos") or {}).get("count") or 0
    print(f"  token logos    -> {logos['count']} cached from {logos['source']} · "
          f"{mirrored} mirrored on this node ({token_media.LOGO_DIR}, "
          f"served at {token_media.LOGO_ROUTE}/); refresh: "
          f"POST /api/tokens/images/refresh", flush=True)
    pending = token_media.mirror_in_background()
    if pending.get("started"):
        print("                   copying the logos this node does not have "
              "yet into the mirror (background)", flush=True)


def print_runtime(runtime: dict) -> None:
    """The Ollama line, with the truth about what this process did."""
    if not runtime:
        print("  Ollama         -> skipped (--no-ollama)", flush=True)
        return
    if runtime.get("serving"):
        if runtime.get("remote"):
            state = "hosted"
        elif runtime.get("started"):
            state = "installed + started by this command"
        else:
            state = "already up"
        print(f"  Ollama         -> {runtime.get('api_url')} ({state}"
              f"{', v' + str(runtime.get('version')) if runtime.get('version') else ''})",
              flush=True)
        if runtime.get("adopted_url"):
            print("                    no local `ollama` binary and no "
                  "ONENESS_OLLAMA_URL was set — adopted the hosted brain "
                  f"(ONENESS_OLLAMA_URL={runtime['adopted_url']})", flush=True)
        if runtime.get("pulled"):
            print(f"  model          -> pulled {runtime['pulled'].get('model')}", flush=True)
        elif runtime.get("pull_error"):
            print(f"  model          -> pull failed: {runtime['pull_error']}", flush=True)
        return
    if runtime.get("remote"):
        print(f"  Ollama         -> hosted {runtime.get('api_url')} — "
              f"{runtime.get('error') or 'not answering'}", flush=True)
        if runtime.get("adopted_url"):
            print("                    no local `ollama` binary and no "
                  "ONENESS_OLLAMA_URL was set — adopted the hosted brain "
                  f"(ONENESS_OLLAMA_URL={runtime['adopted_url']})", flush=True)
        if not ollama.api_key():
            print(f"                   {ollama.auth_error_hint()}", flush=True)
        return
    if runtime.get("installed"):
        print(f"  Ollama         -> installed but not serving: "
              f"{runtime.get('error') or 'unknown error'}", flush=True)
        return
    print(f"  Ollama         -> NOT installed — {runtime.get('error') or 'no installer output'}",
          flush=True)
    if ollama.is_local():
        print("                   open /ollama and press “bootstrap”, or install from "
              f"{ollama.DOWNLOAD_URL}", flush=True)


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OneNess TOR node (API + pages)")
    parser.add_argument("--host", help="interface to bind (default: api.host or $PORT)")
    parser.add_argument("--port", type=int, help="port to bind (default: $PORT or api.port)")
    parser.add_argument("--debug", action="store_true", help="Flask debug server")
    parser.add_argument("--no-sweep", action="store_true",
                        help="kept for compatibility: nothing is swept any more")
    parser.add_argument("--no-sync", action="store_true",
                        help="do not pull from sync.origin_url on a timer")
    parser.add_argument("--no-ollama", action="store_true",
                        help="do not install/start Ollama at boot")
    parser.add_argument("--no-node", action="store_true",
                        help="do not start a co-located Rust ledger "
                             "(node.api_url stays whatever it is)")
    parser.add_argument("--ollama-pull", action="store_true",
                        help="pull ollama.model when it is missing (several GB)")
    args = parser.parse_args(argv)

    host, port = resolve_bind(args.host, args.port)
    os.environ.setdefault("ONENESS_API_PORT", str(port))
    # Never pin the *public* URL here: on Render/Docker the process binds
    # 0.0.0.0:$PORT while the browser uses the forwarded https host, and a local
    # run uses 127.0.0.1. base_url.request_base() resolves it per request; this
    # internal URL is only the default for in-process tool calls.
    os.environ.setdefault("ONENESS_INTERNAL_BASE_URL", f"http://127.0.0.1:{port}")
    # The chain admin token nobody has to type: created once in
    # data/admin/chain_token, and adopted by the ledger *before* it starts, so
    # sealing the archive or moving the fee receiver works from /admin with just
    # the username and password.
    held = chain_token.ensure()
    where = ("the origin's (replica)" if held["source"] == "origin"
             else (held["path"] or "not configured"))
    print(f"  chain token    -> {'created' if held['created'] else 'ready'} "
          f"({where}"
          f"{', adopted by the ledger' if held['adopted'] else ''})", flush=True)
    if not chain_token.usable():
        print("  chain token    -> the ledger already answers to another token: "
              "restart it, or set ONENESS_ADMIN_TOKEN, or chain-level actions "
              "stay refused", flush=True)
    # The ledger first: every page and every tool call reads the chain through
    # core.node_client, so booting the API against a node nobody started is how a
    # deployment ends up 503-ing on every chain route. A remote node.api_url is
    # left alone; a loopback one is hosted here (verified seed included).
    ledger = ledger_host.ensure_local_node(
        autostart=False if args.no_node else None,
        wait=float(settings.get("node.start_timeout") or 25.0))
    app = create_app()
    banner(f"http://{host}:{port}", ledger)

    sync_thread = None if args.no_sync else node_sync.start_scheduler()
    wiring = node_sync.status()
    if sync_thread is not None:
        print(f"  sync           -> replica of {wiring['url']} every "
              f"{wiring['interval_seconds']}s (POST /api/chain/sync/pull)", flush=True)
    elif wiring["configured"]:
        print("  sync           -> timer disabled (--no-sync or sync.auto: false)",
              flush=True)
    else:
        print("  sync           -> origin (no sync.origin_url; snapshot export is "
              "at /api/chain/sync/snapshot)", flush=True)

    push_thread = None if args.no_sync else node_sync.start_pusher()
    pushing = wiring.get("push") or {}
    if push_thread is not None:
        print(f"  snapshot push  -> {len(pushing.get('targets') or [])} target(s) "
              f"every {pushing.get('interval_seconds')}s "
              f"(POST /api/chain/sync/push; copies in {pushing.get('save_dir')})",
              flush=True)
    elif (pushing.get("targets") or []):
        print("  snapshot push  -> configured but not started (replica, "
              "sync.push.enabled: false or --no-sync)", flush=True)
    else:
        print("  snapshot push  -> no targets (sync.push.targets) — nothing to "
              "push to yet", flush=True)

    # Tor before the onion: a control port nobody opened is the whole reason an
    # onion deploy does not work. Only when asked (tor.auto_start).
    tor_runtime: dict = {}
    if bool(settings.get("tor.auto_start")):
        if not tor_service.binary() and bool(settings.get("tor.auto_install")):
            # A service created from a Python runtime (instead of this repo's
            # Dockerfile) has no tor package at all: install it here and say
            # exactly what ran, rather than logging "not installed" every boot.
            try:
                installed = tor_service.install_daemon()
                print(f"  tor            -> {installed.get('note')}", flush=True)
            except AppError as problem:
                print(f"  tor            -> not installed: {problem.message}",
                      flush=True)
        try:
            tor_runtime = tor_service.ensure_daemon()
            print(f"  tor            -> {tor_runtime.get('note')}"
                  f" (control {tor_runtime.get('control_port')}"
                  f"{', pid ' + str(tor_runtime.get('pid')) if tor_runtime.get('pid') else ''})",
                  flush=True)
        except AppError as problem:
            print(f"  tor            -> not started: {problem.message}", flush=True)

    if bool(settings.get("tor.publish_node")):
        try:
            published = tor_service.publish_node(port)
            print(f"  onion          -> {published.get('url')}"
                  f"{' (reused)' if published.get('reused') else ''}", flush=True)
            if published.get("key_warning"):
                print(f"  onion key      -> {published['key_warning']}", flush=True)
            key = tor_service.onion_key_env()
            if key and not str(settings.get("tor.onion_key") or "").strip():
                print("                   persist this key as "
                      "ONENESS_TOR_ONION_KEY to keep the same address after a "
                      "redeploy (GET /api/tor/key, admin only)", flush=True)
        except AppError as problem:
            print(f"  onion          -> not published: {problem.message}", flush=True)
            # The key is the identity: say which .onion this deployment owns even
            # when no daemon answered, so the address is never a mystery.
            owned = tor_service.node_key_url()
            if owned:
                print(f"                   the key on record owns {owned} — it is "
                      f"published the moment a control port answers", flush=True)

    runtime: dict = {}
    if not args.no_ollama:
        runtime = ollama_runtime(
            auto_install=bool(settings.get("ollama.auto_install")),
            pull=bool(args.ollama_pull or settings.get("ollama.pull_on_start")),
            wait=float(settings.get("ollama.start_timeout") or 45.0))
        print_runtime(runtime)
    else:
        print_runtime({})

    serve(app, host, port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


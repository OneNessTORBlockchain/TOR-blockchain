"""``torctl`` — the OneNess TOR command line (the TOR CLI).

    python -m oneness_tor.cli status
    python -m oneness_tor.cli start
    python -m oneness_tor.cli publish --port 8090
    python -m oneness_tor.cli key            # ONENESS_TOR_ONION_KEY=…
    python -m oneness_tor.cli keygen         # mint a key + the .onion it owns
    python -m oneness_tor.cli install        # install the tor package (apt/apk/…)
    python -m oneness_tor.cli newnym
    python -m oneness_tor.cli services
    python -m oneness_tor.cli sync --push
    python -m oneness_tor.cli snapshot
    python -m oneness_tor.cli fees list
    python -m oneness_tor.cli fees set meme_banner_7d 45
    python -m oneness_tor.cli fees reset meme_banner_7d
    python -m oneness_tor.cli health --base http://127.0.0.1:8090

It calls this process's own modules, not a second copy of the logic:
``tor_service`` publishes the onion, ``node_sync`` moves the snapshot,
``core.pricing`` is the one price table. So a command either did the real thing
or says exactly why it could not — there is no wrapper that fakes success.

Every command accepts ``--json`` (machine-readable output) and exits non-zero on
failure, which is what makes it usable from a deploy script or a cron job.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from core import pricing
from core.errors import AppError


def _emit(payload, as_json: bool, lines: list[str] | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return
    for line in lines or []:
        print(line)


def _status(args) -> int:
    from oneness_tor.services import node_sync, tor_service

    tor = tor_service.status()
    node = tor.get("node") or {}
    sync = node_sync.status()
    pushing = sync.get("push") or {}
    payload = {"tor": tor, "onion": node.get("url"), "node_port": node.get("port"),
               "role": sync.get("role"), "origin": sync.get("url") or None,
               "push": pushing}
    lines = [
        f"tor            : {'reachable' if tor.get('reachable') else 'DOWN'}"
        f" ({tor.get('version') or tor.get('error') or 'no daemon'})",
        f"control port   : {tor.get('control_port')} "
        f"({'open' if tor.get('control_open') else 'closed'})"
        f" · binary {tor.get('binary') or 'not installed'}",
        f"onion          : {node.get('url') or 'not published'}"
        f"{' (key set)' if node.get('key_set') else ''}"
        f"{' · key owns ' + str(node.get('key_address')) if node.get('key_address') and not node.get('url') else ''}",
        f"node port      : {node.get('port')}",
        f"role           : {sync.get('role')} "
        f"({'origin' if sync.get('role') == 'origin' else 'replica of ' + str(sync.get('url'))})",
        f"snapshot push  : {'on' if pushing.get('enabled') else 'off'}"
        f" · {len(pushing.get('targets') or [])} target(s)"
        f" · every {pushing.get('interval_seconds')}s"
        f" · copies in {pushing.get('save_dir')}",
        f"last push      : {(pushing.get('last_push') or {}).get('reason') or 'never'}",
    ]
    _emit(payload, args.json, lines)
    return 0


def _start(args) -> int:
    from oneness_tor.services import tor_service

    runtime = tor_service.ensure_daemon(wait=args.wait, force=args.force)
    _emit(runtime, args.json, [
        f"tor            : {runtime.get('note')}",
        f"control port   : {runtime.get('control_port')}",
        f"socks port     : {runtime.get('socks_port') or '-'}",
        f"torrc          : {runtime.get('torrc') or tor_service.torrc_path()}",
        f"pid            : {runtime.get('pid') or '-'}",
    ])
    return 0


def _publish(args) -> int:
    from oneness_tor.services import tor_service

    published = tor_service.publish_node(args.port, refresh=args.refresh)
    key = tor_service.onion_key_env()
    lines = [
        f"onion          : {published.get('url')}"
        f"{' (reused)' if published.get('reused') else ''}",
        f"service id     : {published.get('service_id')}",
        f"published at   : http://127.0.0.1:{published.get('port')}",
        f"browse it      : Tor Browser → {published.get('url')}",
        f"persist key    : ONENESS_TOR_ONION_KEY={key or '(none)'}",
    ]
    if published.get("key_warning"):
        lines.append(f"key mismatch   : {published['key_warning']}")
    elif published.get("key_address"):
        lines.append(f"key owns       : http://{published['key_address']}")
    _emit(published, args.json, lines)
    return 0


def _unpublish(args) -> int:
    from oneness_tor.services import tor_service

    removed = tor_service.unpublish_node()
    _emit(removed, args.json, [f"removed        : {removed.get('onion')}",
                              f"service id     : {removed.get('removed')}"])
    return 0


def _newnym(args) -> int:
    from oneness_tor.services import tor_service

    result = tor_service.new_identity()
    _emit(result, args.json, [f"NEWNYM        : {result.get('reply')}"])
    return 0


def _key(args) -> int:
    from oneness_tor.services import tor_service

    blob = tor_service.onion_key_env()
    address = tor_service.onion_address(blob)
    payload = {"onion": tor_service.node_url() or None,
               "key_address": f"http://{address}" if address else None,
               "env": "ONENESS_TOR_ONION_KEY", "key": blob}
    _emit(payload, args.json, [
        f"onion          : {payload['onion'] or 'not published'}",
        f"key owns       : {payload['key_address'] or '(no key on record)'}",
        "this is the value ONENESS_TOR_ONION_KEY must hold (dashboard or .env):",
        f"ONENESS_TOR_ONION_KEY={blob or '(no key yet — publish or keygen first)'}",
    ])
    return 0


def _keygen(args) -> int:
    """Mint an onion key offline, so the address exists before the first deploy."""
    from oneness_tor.services import tor_service

    made = tor_service.keygen()
    lines = [
        f"onion          : {made['url']}",
        "this blob is what Tor returns from ADD_ONION NEW:ED25519-V3 — commit it,",
        "or set it in the dashboard, and every redeploy re-creates this address:",
        f"ONENESS_TOR_ONION_KEY={made['key']}",
        "then: ONENESS_TOR_AUTOSTART=1 ONENESS_TOR_PUBLISH_NODE=1 "
        "(the default once it is in .env)",
    ]
    if args.show_seed:
        lines.insert(1, f"seed (hex)     : {made['seed_hex']}")
    _emit(made, args.json, lines)
    return 0


def _install(args) -> int:
    from oneness_tor.services import tor_service

    runtime = tor_service.install_daemon()
    lines = [f"tor            : {runtime.get('note')}",
             f"binary         : {runtime.get('binary')}"]
    for row in runtime.get("commands") or []:
        lines.append(f"  $ {row['command']} -> {row['exit']}")
    if args.start:
        started = tor_service.ensure_daemon(wait=args.wait)
        lines.append(f"daemon         : {started.get('note')} "
                     f"(control {started.get('control_port')})")
    _emit(runtime, args.json, lines)
    return 0


def _services(args) -> int:
    from oneness_tor.services import tor_service

    rows = [{"service_id": row.get("service_id"), "onion": row.get("onion"),
             "label": row.get("label"), "path": row.get("path"),
             "created_at": row.get("created_at")}
            for row in tor_service.SERVICES.all()]
    _emit({"services": rows, "count": len(rows)}, args.json,
          [f"{len(rows)} recorded service(s)"] +
          [f"  {row['onion']}  {row['label']}  {row['path']}" for row in rows])
    return 0


def _sync(args) -> int:
    from oneness_tor.services import node_sync

    if args.push:
        targets = [part for part in (args.targets or "").split(",") if part.strip()]
        report = node_sync.push(targets=targets or None, force=args.force,
                                save=False if not args.save else None)
        _emit(report, args.json, [
            f"height         : {report.get('origin_height')}",
            f"pushed         : {report.get('pushed')}/{len(report.get('targets') or [])}",
            f"saved          : {report.get('saved') or '(not saved)'}",
        ] + [f"  {row['url']}: {row.get('reason')}" for row in report["targets"]])
        return 0
    if args.pull:
        report = node_sync.pull(force=args.force)
        _emit(report, args.json, [
            f"origin         : {report.get('origin')}",
            f"imported       : {report.get('imported')} · {report.get('reason')}",
        ])
        return 0
    status = node_sync.status()
    _emit(status, args.json, [
        f"role           : {status.get('role')}",
        f"origin         : {status.get('url') or '(this node proposes)'}",
        f"last pull      : {(status.get('last_pull') or {}).get('reason')}",
        f"last push      : {((status.get('push') or {}).get('last_push') or {}).get('reason')}",
    ])
    return 0


def _snapshot(args) -> int:
    from oneness_tor.services import node_sync

    document = node_sync.snapshot()
    verified = node_sync.verify_snapshot(document)
    path = args.out or node_sync.save_snapshot(document)
    _emit({"path": str(path), "height": verified["height"],
           "head_hash": verified["head_hash"], "tokens": verified["tokens"],
           "accounts": verified["accounts"]}, args.json, [
        f"height         : {verified['height']}",
        f"head hash      : {verified['head_hash']}",
        f"tokens/accounts: {verified['tokens']}/{verified['accounts']}",
        f"written        : {path}",
    ])
    return 0


def _fees(args) -> int:
    if args.action == "set":
        row = pricing.set_price(args.code, args.tor,
                                confirm_locked=args.confirm_locked, actor="torctl")
        _emit(row, args.json, [
            f"{row['code']:<22} {row['tor']:>10g} TOR "
            f"(was {row.get('previous_tor')}, default {row['default_tor']:g})"])
        return 0
    if args.action == "reset":
        if args.code:
            row = pricing.reset_price(args.code, actor="torctl")
            _emit(row, args.json, [f"{row['code']:<22} {row['tor']:>10g} TOR "
                                  f"({row['source']})"])
        else:
            document = pricing.reset_all(actor="torctl")
            _emit(document, args.json,
                  [f"reset {document['count']} row(s) to the published table"])
        return 0
    document = pricing.admin_table()
    lines = [f"{'code':<22}{'price':>12}  {'default':>10}  source"]
    for row in document["rows"]:
        lines.append(f"{row['code']:<22}{row['tor']:>10g} TOR"
                     f"{row['default_tor']:>10g}   {row['source']}"
                     f"{'  (LOCKED)' if row['locked'] else ''}")
    _emit(document, args.json, lines)
    return 0


def _health(args) -> int:
    base = (args.base or "").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as problem:
        print(f"error: {base}/api/health is not reachable ({problem})", file=sys.stderr)
        return 1
    data = payload.get("data") or {}
    _emit(payload, args.json, [
        f"status         : {data.get('status')}",
        f"height         : {data.get('height')}",
        f"chain id       : {data.get('chain_id')}",
        f"node           : {'up' if data.get('node_reachable') else 'DOWN'} "
        f"({data.get('node_url')})",
        f"onion          : {data.get('onion') or 'not published'}",
    ])
    return 0




def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torctl",
        description="OneNess TOR command line: the Tor daemon, the onion, the "
                    "snapshot push and the fee schedule")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Tor, the onion, the sync role and the push timer")
    start = sub.add_parser("start", help="start the Tor daemon (tor.auto_start)")
    start.add_argument("--wait", type=float, default=None,
                       help="seconds to wait for the control port")
    start.add_argument("--force", action="store_true", help="restart even if it answers")
    publish = sub.add_parser("publish", help="ADD_ONION for this API's port")
    publish.add_argument("--port", type=int, default=None,
                         help="target port (default: the bound API port)")
    publish.add_argument("--refresh", action="store_true",
                         help="ADD_ONION again even when an onion is on record")
    sub.add_parser("unpublish", help="DEL_ONION the service that publishes this API")
    sub.add_parser("newnym", help="ask Tor for a new circuit identity")
    sub.add_parser("key", help="print ONENESS_TOR_ONION_KEY for this onion")
    keygen = sub.add_parser("keygen",
                            help="mint an onion key offline + the .onion it owns")
    keygen.add_argument("--show-seed", dest="show_seed", action="store_true",
                        help="also print the 32-byte seed the key came from")
    install = sub.add_parser("install",
                             help="install the tor package (apt-get/apk/dnf/yum/brew)")
    install.add_argument("--start", action="store_true",
                         help="start the daemon once it is installed")
    install.add_argument("--wait", type=float, default=None,
                         help="seconds to wait for the control port")
    sub.add_parser("services", help="every hidden service this node recorded")

    sync = sub.add_parser("sync", help="replication status / pull / push")
    sync.add_argument("--push", action="store_true", help="push the snapshot now")
    sync.add_argument("--pull", action="store_true", help="pull from the origin now")
    sync.add_argument("--targets", default="",
                      help="comma-separated replica URLs for this push")
    sync.add_argument("--force", action="store_true", help="adopt/overwrite anyway")
    sync.add_argument("--no-save", dest="save", action="store_false",
                      help="do not write data/snapshots/latest.json")

    snap = sub.add_parser("snapshot", help="export, verify and save the ledger snapshot")
    snap.add_argument("--out", default=None, help="write here instead of data/snapshots")

    fees = sub.add_parser("fees", help="list / set / reset the service fees")
    fees.add_argument("action", nargs="?", default="list",
                      choices=["list", "set", "reset"])
    fees.add_argument("code", nargs="?", help="price code (contract_create, mint, …)")
    fees.add_argument("tor", nargs="?", type=float, help="new price in TOR")
    fees.add_argument("--confirm-locked", dest="confirm_locked", action="store_true",
                      help="move a LOCKED Section 8 row deliberately")

    health = sub.add_parser("health", help="GET /api/health of a running node")
    health.add_argument("--base", default="http://127.0.0.1:8090")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"
    handlers = {"status": _status, "start": _start, "publish": _publish,
                "unpublish": _unpublish, "newnym": _newnym, "key": _key,
                "keygen": _keygen, "install": _install,
                "services": _services, "sync": _sync, "snapshot": _snapshot,
                "fees": _fees, "health": _health}
    handler = handlers.get(command)
    if handler is None:                        # pragma: no cover - argparse guards
        parser.print_help()
        return 2
    if command == "fees" and args.action == "set" and (not args.code
                                                      or args.tor is None):
        parser.error("fees set needs a code and a price: fees set mint 4.5")
    try:
        return handler(args)
    except AppError as problem:
        print(f"error {problem.code}: {problem.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

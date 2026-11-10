"""Keep every replica pointed at the live public URL of the proposing node.

The origin is the public Render node (``config.fleet.yaml`` → ``origin.url`` =
``https://www.torblockchain.fun``), so its URL does not move. This command exists
for the two cases that remain — a verified origin URL, and an origin that has to be
moved (a new domain, or a Cloudflare *quick* tunnel for the laptop, whose hostname
changes on every restart). One command does three things: write the URL, prove it
serves the ledger, and say what to commit:

    python scripts/fleet_origin.py --show
    python scripts/fleet_origin.py --verify
    python scripts/fleet_origin.py --set-url https://<new>.trycloudflare.com --verify
    git add config.fleet.yaml && git commit -m "fleet: origin url" && git push

``scripts/origin_tunnel.ps1`` runs exactly that sequence after starting
cloudflared. ``--verify`` really fetches ``GET /api/chain/sync/snapshot`` with the
fleet token and reports the height it served, so a dead tunnel, an unset
``ONENESS_ADMIN_TOKEN`` on the origin, or a typo is caught here instead of on a
replica that then logs SYNC_ORIGIN_UNREACHABLE.
The token is a chain credential: it is printed masked, never in full.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FLEET = ROOT / "config.fleet.yaml"


def verify(base: str, token: str, timeout: float = 30.0) -> dict:
    """Fetch the origin's snapshot exactly the way a replica does."""
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chain/sync/snapshot",
        headers={"accept": "application/json", "x-admin-token": token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = (payload or {}).get("error") or {}
        raise SystemExit(f"the origin refused the snapshot: {error.get('message')}")
    return payload.get("data") or {}


def _replace_in_block(lines: list[str], block: str, key: str, value: str) -> bool:
    """Rewrite ``key:`` inside a top-level block, leaving every comment intact."""
    start = None
    for index, line in enumerate(lines):
        if line.startswith((" ", "\t")):
            continue
        if re.match(rf"^{re.escape(block)}\s*:\s*(#.*)?$", line.strip()):
            start = index
            break
    if start is None:
        return False
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            break                       # the next top-level block
        match = re.match(rf"^(\s+){re.escape(key)}\s*:\s*.*$", line.rstrip("\n"))
        if match:
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{match.group(1)}{key}: {value}{newline}"
            return True
    return False


def _set_dotenv(path: Path, key: str, value: str) -> bool:
    """Rewrite ``KEY=value`` in the tracked dotenv — it wins over the YAML file."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{key}={value}{newline}"
            found = True
    if not found:
        lines.append(f"{key}={value}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true",
                        help="print the committed wiring (the default)")
    parser.add_argument("--set-url", metavar="URL", dest="set_url",
                        help="new public URL of the origin")
    parser.add_argument("--token", help="origin chain admin token "
                                       "(default: the committed one)")
    parser.add_argument("--verify", action="store_true",
                        help="fetch /api/chain/sync/snapshot from that URL")
    parser.add_argument("--wait", type=int, default=None, metavar="SECONDS",
                        help="retry the verification for up to SECONDS "
                             "(default 60 with --set-url, otherwise 0): a fresh "
                             "quick-tunnel hostname needs a few seconds of DNS")
    args = parser.parse_args(argv)

    text = FLEET.read_text(encoding="utf-8")
    origin = ((yaml.safe_load(text) or {}).get("origin") or {})
    url = str(args.set_url or origin.get("url") or "").strip().rstrip("/")
    token = str(args.token or origin.get("token") or "").strip()

    if args.set_url:
        lines = text.splitlines(keepends=True)
        if not _replace_in_block(lines, "origin", "url", url):
            print("config.fleet.yaml has no origin.url line — edit it by hand",
                  file=sys.stderr)
            return 2
        if args.token:
            _replace_in_block(lines, "origin", "token", token)
            _replace_in_block(
                lines, "origin", "admin_token_sha256",
                hashlib.sha3_256(token.encode("utf-8")).hexdigest())
        FLEET.write_text("".join(lines), encoding="utf-8")
        # the tracked dotenv wins over the YAML file (real env vars win over both),
        # so the two must never disagree about where the origin lives
        dotenv = ROOT / ".env"
        if dotenv.exists():
            _set_dotenv(dotenv, "ONENESS_SYNC_ORIGIN", url)
            if args.token:
                _set_dotenv(dotenv, "ONENESS_SYNC_TOKEN", token)
        print(f"config.fleet.yaml + .env: origin.url = {url}")

    if not url:
        print("no origin URL: config.fleet.yaml has an empty origin.url",
              file=sys.stderr)
        return 2

    masked = ("*" * max(0, len(token) - 4) + token[-4:]) if token else "(none)"
    print(f"origin url   : {url}")
    print(f"origin token : {masked}")
    if args.verify or args.set_url:
        wait = args.wait if args.wait is not None else (60 if args.set_url else 0)
        deadline = time.time() + max(0, wait)
        while True:
            try:
                data = verify(url, token)
                break
            except (urllib.error.URLError, OSError, ValueError) as problem:
                if time.time() >= deadline:
                    print(f"UNREACHABLE  : {url} ({problem})", file=sys.stderr)
                    return 1
                print(f"waiting      : {problem} - a fresh hostname needs DNS")
                time.sleep(5)
        print(f"verified     : height={data.get('height')} "
              f"head={str(data.get('head_hash'))[:12]}… "
              f"blocks={len(data.get('blocks') or [])}")
    if args.set_url:
        print("\ncommit it so Render redeploys the replicas:\n"
              "  git add config.fleet.yaml .env && "
              "git commit -m \"fleet: origin url\" && git push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

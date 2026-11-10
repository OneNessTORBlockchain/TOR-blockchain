"""Re-export the live chain snapshot into ``seed/chain-snapshot.json``.

    python tools/export_snapshot.py [--port 8092]

A fresh container (Render, Docker, a VPS) adopts this document before the Rust
node starts, so a new deployment opens at the same height, with the same 120
tokens, the 170 seeded pools and every balance — no manual replay.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402

TARGET = ROOT / "seed" / "chain-snapshot.json"


def fetch(port: int, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/chain/sync/snapshot",
        headers={"x-admin-token": token})
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise SystemExit(f"the API refused the export: {body.get('error')}")
    return body["data"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--token", default=str(settings.get("api.admin_token")))
    args = parser.parse_args()

    snapshot = fetch(args.port, args.token)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {TARGET}  height={snapshot.get('height')}  "
          f"accounts={snapshot.get('accounts')}  tokens={snapshot.get('tokens')}  "
          f"size={TARGET.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

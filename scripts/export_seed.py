#!/usr/bin/env python
"""Turn the local ledger into a verified seed snapshot a fresh node can adopt.

A container has no chain: ``data/chain`` is gitignored, so a Render/Docker
deployment starts against an empty directory and every chain route answers
``503 CHAIN_UNAVAILABLE``. This script freezes the chain you already have into
``seed/chain-snapshot.json`` (state + every block, no key material), which
:mod:`core.ledger_host` re-verifies — every block hash, every ``prev_hash`` link,
the head hash and the state root — before it writes one byte.

    python scripts/export_seed.py
    python scripts/export_seed.py --out seed/chain-snapshot.json

The result is the same JSON the node serves at ``GET /api/admin/sync/export``, so
it can also be pushed to a host and used through ``ONENESS_NODE_SEED=<https URL>``
instead of committing it. Signatures, addresses and notes are public chain data
(they are what ``/api/public/ledger`` serves); the admin token appears only as
its ``sha3-256`` digest and is replaced by the receiving node's own token.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import settings                    # noqa: E402
from oneness_tor.services import node_sync          # noqa: E402


def build(chain_dir: Path) -> dict:
    """Assemble the snapshot exactly as the Rust node exports it."""
    state_path = chain_dir / "state.json"
    if not state_path.is_file():
        raise SystemExit(f"no chain at {chain_dir} — start `torchain serve --dir "
                         f"{chain_dir}` once, or point --chain-dir at the ledger")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    blocks: list[dict] = []
    blocks_path = chain_dir / "blocks.jsonl"
    if blocks_path.is_file():
        for index, line in enumerate(
                blocks_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                blocks.append(json.loads(line))
            except json.JSONDecodeError as problem:
                raise SystemExit(f"blocks.jsonl line {index}: {problem}") from None
    return {"standard": node_sync.SNAPSHOT_STANDARD,
            "chain_id": state.get("chain_id"),
            "genesis_hash": state.get("genesis_hash"),
            "head_hash": state.get("head_hash"),
            "height": int(state.get("height") or 0),
            "state_root": node_sync.state_root_of(state),
            "tokens": len(state.get("tokens") or {}),
            "accounts": len(state.get("accounts") or {}),
            "records": len(state.get("records") or []),
            "state": state,
            "blocks": blocks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the local ledger as a verified seed snapshot")
    parser.add_argument("--chain-dir", default=str(settings.chain_dir),
                        help="ledger directory (default: chain.data_dir)")
    parser.add_argument("--out", default=str(ROOT / "seed" / "chain-snapshot.json"),
                        help="where to write the snapshot")
    args = parser.parse_args(argv)

    snapshot = build(Path(args.chain_dir))
    verified = node_sync.verify_snapshot(snapshot)   # refuses to write a bad seed
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"verified seed -> {out}")
    print(f"  chain {snapshot.get('chain_id')}  height {verified['height']}  "
          f"head {verified['head_hash'][:16]}...  tokens {verified['tokens']}  "
          f"accounts {verified['accounts']}  records {verified['records']}")
    print(f"  {out.stat().st_size} bytes, no key material")
    print("  a fresh node adopts it from seed/chain-snapshot.json, or from "
          "ONENESS_NODE_SEED=<path|https URL>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

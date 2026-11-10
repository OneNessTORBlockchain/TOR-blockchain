#!/usr/bin/env python
"""Create the top-N market-cap tokens on the OneNess TOR chain.

    python tools/bootstrap_tokens.py                    # plan only (default)
    python tools/bootstrap_tokens.py --run              # mint for real
    python tools/bootstrap_tokens.py --run --limit 100 --pools
    python tools/bootstrap_tokens.py --run --node http://127.0.0.1:8545

The ranked list comes from the cached public metadata
(``data/metadata/token-images.json``, CoinGecko top coins by market cap). Each
new ticker is a real signed ``token_create`` transaction (the chain charges
5 TOR), existing tickers are skipped, and every replica shows the result after
``POST /api/chain/sync/pull``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint the top-N market-cap tokens on the TOR chain")
    parser.add_argument("--limit", type=int, default=100,
                        help="how many ranked tokens (1-250, default 100)")
    parser.add_argument("--supply", type=int, default=1_000_000,
                        help="whole-token supply for each new token")
    parser.add_argument("--wallet", default="founder",
                        help="keystore wallet that signs and pays the 5 TOR fee")
    parser.add_argument("--node", default="",
                        help="Rust node base URL (default: node.api_url)")
    parser.add_argument("--admin-token", default="",
                        help="chain admin token (default: api.admin_token)")
    parser.add_argument("--pools", action="store_true",
                        help="also seed a TOR pool per token (10 TOR each)")
    parser.add_argument("--pool-tor", type=float, default=10.0,
                        help="TOR liquidity per pool when --pools is used")
    parser.add_argument("--run", action="store_true",
                        help="mint for real (without it this is a dry run)")
    args = parser.parse_args(argv)

    # env wins over config.yaml, so set it before the services import settings
    if args.node:
        os.environ["ONENESS_NODE_API"] = args.node
    if args.admin_token:
        os.environ["ONENESS_ADMIN_TOKEN"] = args.admin_token

    from oneness_tor.services import token_bootstrap  # noqa: E402

    report = token_bootstrap.run(limit=args.limit, supply=args.supply,
                                 wallet=args.wallet, dry_run=not args.run,
                                 pools=args.pools, pool_tor=args.pool_tor)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("failed"):
        print(f"\n{len(report['failed'])} failure(s) — see the list above",
              file=sys.stderr)
        return 1
    if not args.run:
        print("\ndry run — re-run with --run to mint", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

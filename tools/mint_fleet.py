#!/usr/bin/env python
"""Mint the 120-token fleet: one wallet and one token per asset.

    python tools/mint_fleet.py                      # plan only (default)
    python tools/mint_fleet.py --run                # 120 wallets, 120 tokens
    python tools/mint_fleet.py --run --node http://127.0.0.1:8601 \\
        --admin-token <token> --fund-tor 6
    python tools/mint_fleet.py --run --start 121 --pin   # re-mint: 120 NEW wallets
    python tools/mint_fleet.py --document           # rewrite docs/TOKEN_FLEET.md

The 120 assets are the top 100 by market cap plus the top 20 Solana memecoins,
fetched from key-less public providers with two fallbacks each
(``oneness_tor/services/market_feed.py``). Each token is minted by its own freshly
generated Ed25519 wallet with the provider's total supply, and the provider's
image is used both for the launcher metadata and for this node's logo cache, so
every page shows the same picture.
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
        description="Mint 120 tokens into 120 wallets (top 100 + top 20 Solana memecoins)")
    parser.add_argument("--majors", type=int, default=100,
                        help="how many top-100 assets (default 100)")
    parser.add_argument("--memes", type=int, default=20,
                        help="how many Solana memecoins (default 20)")
    parser.add_argument("--fund-tor", type=float, default=6.0,
                        help="TOR sent to every new wallet (the chain charges 5 per token)")
    parser.add_argument("--founder", default="founder",
                        help="wallet that funds the fleet (default founder)")
    parser.add_argument("--prefix", default="fleet",
                        help="wallet-name prefix: fleet001, fleet002… (default fleet)")
    parser.add_argument("--start", type=int, default=1,
                        help=("first wallet number; a re-mint passes the next free "
                              "one (e.g. --start 121 → fleet121…fleet240, i.e. 120 "
                              "brand-new key pairs and addresses)"))
    parser.add_argument("--node", default="",
                        help="Rust node base URL (default: node.api_url)")
    parser.add_argument("--admin-token", default="",
                        help="chain admin token (default: api.admin_token)")
    parser.add_argument("--pin", action="store_true",
                        help="also pin every token's metadata to IPFS (Pinata)")
    parser.add_argument("--refresh", action="store_true",
                        help="force a market-data fetch before planning")
    parser.add_argument("--document", action="store_true",
                        help="only rewrite docs/TOKEN_FLEET.md from the stored manifest")
    parser.add_argument("--run", action="store_true",
                        help="mint for real (without it this is a dry run)")
    args = parser.parse_args(argv)

    # env wins over config.yaml, so set it before the services import settings
    if args.node:
        os.environ["ONENESS_NODE_API"] = args.node
    if args.admin_token:
        os.environ["ONENESS_ADMIN_TOKEN"] = args.admin_token

    from oneness_tor.services import market_feed, token_fleet  # noqa: E402

    if args.document:
        path = token_fleet.write_document()
        print(json.dumps({"document": str(path),
                          "manifest": token_fleet.MANIFEST}, indent=2))
        return 0

    if args.refresh:
        print(json.dumps(market_feed.refresh(), indent=2), file=sys.stderr)

    report = token_fleet.run(majors=args.majors, memes=args.memes,
                             fund_tor=args.fund_tor, founder=args.founder,
                             prefix=args.prefix, start=args.start,
                             dry_run=not args.run, pin_metadata=args.pin)
    if not args.run:
        summary = {key: report.get(key) for key in
                   ("standard", "count", "to_mint", "existing", "estimated_tor",
                    "estimated_tor_min", "affordable", "funding", "fee",
                    "images", "warning")}
        summary["tokens"] = report.get("tokens")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        printed = {key: report.get(key) for key in
                   ("dry_run", "created_count", "failed", "skipped", "manifest",
                    "document", "images_remembered", "elapsed_seconds")}
        printed["created"] = [{key: entry.get(key) for key in
                               ("position", "symbol", "supply", "image", "txid")}
                              for entry in report.get("created") or []]
        print(json.dumps(printed, indent=2, ensure_ascii=False))
    if report.get("failed"):
        print(f"\n{len(report['failed'])} failure(s) — see the list above",
              file=sys.stderr)
        return 1
    if not args.run:
        print("\ndry run — re-run with --run to create the wallets and mint",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

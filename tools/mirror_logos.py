#!/usr/bin/env python
"""Mirror every known token logo onto this node — one local file per ticker.

    python tools/mirror_logos.py                      # copy what is missing
    python tools/mirror_logos.py --symbols BTC,ETH    # just these tickers
    python tools/mirror_logos.py --force              # re-copy everything
    python tools/mirror_logos.py --limit 50           # cap this run
    python tools/mirror_logos.py --data-dir data --json

The chain has no image field, so the node resolves logos from public metadata
(CoinGecko, then DexScreener) and caches them in ``data/metadata/token-images.json``.
This tool copies each picture into ``data/metadata/token-logos/<SYMBOL>.<ext>``;
the API serves that folder at ``/assets/tokens/`` and every page (token table,
launcher, wallet, explorer, admin) draws the local file, so a visitor never needs
CoinGecko to be reachable — over Tor, offline, behind a filter or after the
provider removed the image. New mints call the same code through
``token_media.remember``, so the mirror stays complete as tokens are added.

It is idempotent: a logo already mirrored is skipped (``--force`` re-copies), a
failure is reported per symbol, and nothing is ever invented. Run it after a
metadata refresh, after a mint, or on a schedule — it costs no network call when
there is nothing new.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy every cached token logo into this node's own mirror")
    parser.add_argument("--symbols", default="",
                        help="only these tickers (comma-separated; default: every cached logo)")
    parser.add_argument("--limit", type=int, default=0,
                        help="copy at most N pictures in this run (default: the configured limit)")
    parser.add_argument("--force", action="store_true",
                        help="re-copy logos this node already mirrored")
    parser.add_argument("--data-dir", default="",
                        help="ONENESS_DATA_DIR to use (default: config/environment)")
    parser.add_argument("--json", action="store_true",
                        help="print the raw report instead of a summary")
    args = parser.parse_args(argv)

    if args.data_dir:
        import os
        os.environ["ONENESS_DATA_DIR"] = args.data_dir

    from oneness_tor.services import token_media

    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
    report = token_media.mirror(symbols or None, force=args.force,
                                limit=args.limit or None)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if not report.get("failed") else 1

    print(f"logos  -> {report.get('count', 0)} of {report.get('total', 0)} "
          f"served by this node ({report.get('dir')}) at {report.get('route')}/")
    print(f"copied -> {report.get('mirrored', 0)}   skipped {report.get('skipped', 0)}"
          f"   bytes {report.get('bytes', 0)}")
    if report.get("enabled") is False:
        print(f"off    -> {report.get('note') or 'logo mirroring is disabled'}")
    if report.get("capped"):
        print("capped -> more pictures are waiting; run it again or raise --limit")
    for failure in report.get("failed") or []:
        print(f"  [FAIL] {failure.get('symbol')}: {failure.get('error')}")
    return 0 if not report.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

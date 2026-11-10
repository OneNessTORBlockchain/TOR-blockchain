"""Write a README.md into every platform folder (run after scaffold_apps)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oneness_tor.services.projects import PROJECTS, ONION, CLEARNET  # noqa: E402

BODY = """# {name}

{tagline}

* **Node API**: `http://127.0.0.1:8090` (override with `VITE_TOR_API`)
* **This app**: `http://127.0.0.1:{port}`
* **Tor**: {onion}{path}
* **Clearnet**: {clearnet}{path}

## Features

{features}

## Run

```powershell
npm install
npm run dev        # http://127.0.0.1:{port}
npm run build      # type-checks and bundles into dist/
npm run preview    # serves dist/ on the same port
```

## What it reads from the node

| Endpoint | Why |
| --- | --- |
| `GET /api/market/tokens` | every token, price, market cap, liquidity, logo |
| `GET /api/market/chart/{{symbol}}` | candles for the chart component |
| `GET /api/market/pools` | the 120 TOR pools and 50 USDC books |
| `GET /api/market/quote` · `POST /api/market/swap` | price a fill, then settle it |
| `GET /api/ads` · `POST /api/ads` | banner rotation and the 50 TOR/week slot |
| `GET /api/ai/models` · `POST /api/ai/chat` | AI-NFT ChatBox on free OpenRouter models |

The shared client, wallet, charts, ChatBox and design system live in
`src/tor/` — they are copied from `apps/tor-kit` in the node repository by
`python tools/scaffold_apps.py --force`.

## Branding

Every page carries the Tor onion address, `www.onenesstorblockchain.com`,
`www.onenesstech.ca` and `support@onenesstech.ca`. Platform payments settle to
the **Tor AI-NFT Wallet**.

## Support

{support} · https://www.onenesstech.ca
"""


def main() -> int:
    for project in PROJECTS:
        folder = Path(project["folder"])
        if not folder.exists():
            print(f"skip {project['id']}: {folder} is missing")
            continue
        text = BODY.format(
            name=project["name"], tagline=project["tagline"], port=project["port"],
            onion=ONION, clearnet=CLEARNET, path=project["path"],
            features="\n".join(f"* {item}" for item in project["features"]),
            support="support@onenesstech.ca")
        (folder / "README.md").write_text(text, encoding="utf-8")
        print(f"wrote {folder / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

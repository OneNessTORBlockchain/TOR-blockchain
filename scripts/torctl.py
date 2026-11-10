"""``python scripts/torctl.py <command>`` — the CLI without the module path.

Same code as ``python -m oneness_tor.cli``: this file only puts the repository
root on ``sys.path`` first, so it works from any working directory (a Render
pre-deploy command, a cron job, a shortcut on the desktop).

    python scripts/torctl.py status
    python scripts/torctl.py publish --port 8090
    python scripts/torctl.py fees set mint 4.5 --confirm-locked
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oneness_tor.cli import main  # noqa: E402 - after sys.path

if __name__ == "__main__":
    raise SystemExit(main())

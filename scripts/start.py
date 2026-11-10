#!/usr/bin/env python
"""The single start command — ``python scripts/start.py``.

The same code path as ``python -m oneness_tor`` (both call
:func:`oneness_tor.startup.main`), exposed as a script so Render, a Dockerfile
and a Windows checkout can all use one identical command. Ollama is installed
and served automatically when missing; see ``oneness_tor/startup.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oneness_tor.startup import main  # noqa: E402  (import after sys.path)

if __name__ == "__main__":
    raise SystemExit(main())

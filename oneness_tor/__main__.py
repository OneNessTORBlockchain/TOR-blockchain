"""``python -m oneness_tor`` — the same start command as ``scripts/start.py``.

Both forms run :func:`oneness_tor.startup.main`, so a local run, a Render deploy
and a container all install/serve Ollama and boot the API identically.
"""
from __future__ import annotations

from oneness_tor.startup import main

if __name__ == "__main__":
    raise SystemExit(main())


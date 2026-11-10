"""Copy each platform's page source into the node repo (``apps/<id>/``).

    python tools/snapshot_apps.py

The six apps live in the starter folders the operator downloaded; this keeps a
readable, versioned copy of the one file that makes each app *that* app
(``src/App.tsx``) so a lost Downloads folder never loses the product. The kit in
``apps/tor-kit`` is already the shared source of truth.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oneness_tor.services.projects import PROJECTS  # noqa: E402


def main() -> int:
    for project in PROJECTS:
        source = Path(project["folder"]) / "src" / "App.tsx"
        if not source.exists():
            print(f"skip {project['id']}: {source} is missing")
            continue
        target = ROOT / "apps" / project["id"] / "App.tsx"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"{project['id']:16} {len(source.read_text(encoding='utf-8')):>6} chars -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Find files nothing references - report first, delete only on --delete.

    python tools/audit_unused.py                # the report (changes nothing)
    python tools/audit_unused.py --delete       # delete the *safe* candidates

What counts as "unused" here
---------------------------
A Python module is unused when **no other text file in the repository names it**
by dotted path (``oneness_tor.services.foo``), by module stem (``foo``), or by
file name (``foo.py``). The scan covers ``.py .html .js .json .yaml .yml .md .sh
.ps1`` plus the ``Dockerfile``, ``render.yaml`` and ``pytest.ini``, because a
module can be live from a template, a shell script or a config file.

What is never deleted automatically
-----------------------------------
* **entry points** - ``app.py``, ``cli.py``, ``__main__.py``, ``startup.py`` and
  everything in ``tools/`` and ``scripts/``. A script that nothing imports is a
  script you run by hand: that is not dead code.
* ``tests/`` - a test imports what it tests, so a test is never a candidate.
* ``apps/`` - separate frontends with their own entry points.
* anything under ``data/``, ``seed/``, ``rust/`` or ``AI NFT/``.

That leaves application modules and templates, which is where dead code actually
accumulates. Even then ``--delete`` refuses anything that looks like an entry
point and prints exactly what it removed, so a mistake is one ``git checkout``
away.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"data", "seed", "rust", "AI NFT", "images", ".git", "__pycache__",
             ".pytest_cache", "node_modules", ".venv", "venv", "target", "logs",
             "apps", "tests"}
TEXT_SUFFIXES = {".py", ".html", ".js", ".json", ".yaml", ".yml", ".md", ".sh",
                 ".ps1", ".txt", ".ini", ".toml"}
EXTRA_FILES = ("Dockerfile", "render.yaml", "pytest.ini", "requirements.txt",
               ".dockerignore", "config.example.yaml")
ENTRY_POINTS = {"app.py", "cli.py", "__main__.py", "startup.py", "__init__.py"}


def text_files() -> list[Path]:
    rows: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in EXTRA_FILES:
            rows.append(path)
    return rows


def dotted(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def is_candidate(path: Path) -> bool:
    """An application module or template - never an entry point or a test."""
    try:
        parts = path.relative_to(ROOT).parts
    except ValueError:
        return False
    if not parts:
        return False
    if path.suffix == ".py":
        if path.name in ENTRY_POINTS:
            return False
        if parts[0] in ("tools", "scripts", "tests", "apps"):
            return False
        return True
    if path.suffix == ".html":
        return "templates" in parts
    return False


def build_index() -> dict[Path, str]:
    """Every text file's content, keyed by path, for the reference search."""
    index: dict[Path, str] = {}
    for path in text_files():
        try:
            index[path] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    return index


def references(target: Path, index: dict[Path, str]) -> list[str]:
    """Which other files name ``target`` (by stem, file name or dotted path)."""
    stem = target.stem
    name = target.name
    dotted_path = dotted(target) if target.suffix == ".py" else ""
    # a short generic stem (utils, base, index) is too noisy to be meaningful
    needles = {name} | ({stem} if len(stem) > 3 else set())
    if dotted_path:
        needles.add(dotted_path)
    pattern = re.compile("|".join(re.escape(needle) for needle in sorted(needles)))
    found: list[str] = []
    for path, text in index.items():
        if path == target:
            continue
        if pattern.search(text):
            found.append(str(path.relative_to(ROOT)))
    return found


def audit() -> tuple[list[tuple[Path, list[str]]], dict[str, int]]:
    index = build_index()
    candidates: list[tuple[Path, list[str]]] = []
    counts = {"files": len(index)}
    for target in (path for path in index if is_candidate(path)):
        counts["scanned"] = counts.get("scanned", 0) + 1
        if not references(target, index):
            candidates.append((target, []))
    return candidates, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delete", action="store_true",
                        help="remove the candidates (a git commit makes it revertible)")
    args = parser.parse_args(argv)

    candidates, counts = audit()
    print(f"scanned {counts['scanned']} application modules/templates "
          f"out of {counts['files']} text files")
    if not candidates:
        print("nothing unreferenced - every application module is named somewhere")
        return 0
    print(f"\n{len(candidates)} file(s) nothing references:")
    for path, _ in candidates:
        print(f"  {path.relative_to(ROOT)}")
    if not args.delete:
        print("\nreport only. Re-run with --delete to remove them, then run the "
              "suite:\n  python -m pytest -q")
        return 0
    removed = 0
    for path, _ in candidates:
        if not is_candidate(path):
            continue
        path.unlink(missing_ok=True)
        removed += 1
        print(f"  removed {path.relative_to(ROOT)}")
    print(f"\nremoved {removed} file(s). Now run: python -m pytest -q")
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                      capture_output=True, text=True).stdout.strip():
        print("(git status shows the deletions - one commit, one revert)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Static sanity checks for the templates and the shared JavaScript.

    python tools/check_pages.py

* every inline ``<script>`` block is parsed by ``node --check`` (when node exists),
* every template is rendered by Flask so Jinja errors fail here, not in the browser,
* unbalanced ``<div>`` counts and attribute typos like ``class="`` are reported.

Exit code 1 on any failure — safe to run in CI.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "oneness_tor" / "web" / "templates"
STATIC_JS = ROOT / "oneness_tor" / "web" / "static" / "js" / "app.js"
PAGES = ["/", "/dashboard", "/explorer", "/admin", "/wallet", "/payments",
         "/contracts", "/workbench", "/meme", "/nft",
         "/ollama", "/docs", "/coin"]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check_js(errors: list[str]) -> None:
    node = shutil.which("node")
    if not node:
        print("node not found — skipping JS syntax checks")
        return
    blocks: list[tuple[str, str]] = []
    if STATIC_JS.exists():
        blocks.append(("app.js", STATIC_JS.read_text(encoding="utf-8")))
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for index, block in enumerate(re.findall(r"<script>(.*?)</script>", text, re.S)):
            blocks.append((f"{path.name}#{index}", block))
    for name, code in blocks:
        if not code.strip():
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(code)
            temp = handle.name
        result = subprocess.run([node, "--check", temp], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        Path(temp).unlink(missing_ok=True)
        if result.returncode:
            errors.append(f"JS {name}: {result.stderr.strip()[:400]}")
        else:
            print(f"  js ok   {name} ({len(code)} chars)")


def check_render(errors: list[str]) -> None:
    from oneness_tor.app import create_app

    client = create_app().test_client()
    for path in PAGES:
        response = client.get(path)
        if response.status_code != 200:
            errors.append(f"page {path} -> {response.status_code}")
            continue
        body = response.get_data(as_text=True)
        if "<!DOCTYPE html>" not in body:
            errors.append(f"page {path} is not a full document")
        if re.search(r'class="[^"]*$', body, re.M):
            errors.append(f"page {path} has an unterminated class attribute")
        opens = len(re.findall(r"<div\b", body))
        closes = len(re.findall(r"</div>", body))
        if opens != closes:
            errors.append(f"page {path}: {opens} <div> vs {closes} </div>")
        print(f"  page ok {path} ({len(body)} bytes, {opens} divs)")


def main() -> int:
    errors: list[str] = []
    print("templates:")
    check_render(errors)
    print("javascript:")
    check_js(errors)
    if errors:
        print("\nFAILED:")
        for problem in errors:
            print(" -", problem)
        return 1
    print("\nall template + JS checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Scaffold the six TOR apps: Vite + React + TS shell + the shared tor-kit.

    python tools/scaffold_apps.py

The apps live in the starter folders the operator downloaded; this script only
writes the files that make each one a real, runnable Vite app (package.json,
vite config, tsconfig, index.html, main.tsx) and copies ``apps/tor-kit`` into
``src/tor`` so every platform imports the same client, wallet, charts and
ChatBox. ``--force`` rewrites a shell that already exists.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oneness_tor.services.projects import PROJECTS  # noqa: E402

KIT = ROOT / "apps" / "tor-kit"
KIT_FILES = ("api.ts", "brand.ts", "styles.css", "ui.tsx", "wallet.tsx",
             "chart.tsx", "chat.tsx")

ACCENTS = {
    "tor-onion-swap": ("#00f0d0", "#0aa5ff"),
    "tor-screener": ("#35ff8a", "#00c2a8"),
    "darkweb-crypto": ("#a45bff", "#ff2bd6"),
    "tor-pump": ("#ff7a3d", "#ff2f7a"),
    "tor-charts": ("#4fa8ff", "#7b5cff"),
    "solarix-engine": ("#ffc94a", "#ff7a2f"),
}

PACKAGE = {
    "private": True,
    "version": "0.1.0",
    "type": "module",
    "scripts": {"dev": "vite", "build": "tsc -b && vite build", "preview": "vite preview"},
    "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
    "devDependencies": {
        "@types/react": "^18.3.12",
        "@types/react-dom": "^18.3.1",
        "@vitejs/plugin-react": "^4.3.4",
        "typescript": "^5.6.3",
        "vite": "^5.4.11",
    },
}

TSCONFIG = {
    "compilerOptions": {
        "target": "ES2021",
        "lib": ["ES2021", "DOM", "DOM.Iterable"],
        "module": "ESNext",
        "moduleResolution": "Bundler",
        "jsx": "react-jsx",
        "strict": True,
        "noUnusedLocals": False,
        "skipLibCheck": True,
        "isolatedModules": True,
        "noEmit": True,
        "types": ["vite/client"],
    },
    "include": ["src"],
}

VITE_CONFIG = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: %PORT%, strictPort: false, host: "127.0.0.1" },
  preview: { port: %PORT% },
});
"""

MAIN = """import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ToastProvider } from "./tor/ui";
import { WalletProvider } from "./tor/wallet";
import App from "./App";
import "./tor/styles.css";
import "./tor/accent.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <WalletProvider>
        <App />
      </WalletProvider>
    </ToastProvider>
  </StrictMode>,
);
"""


def accent_css(project_id: str) -> str:
    primary, secondary = ACCENTS[project_id]
    return (
        "/* per-app skin: the same kit, this project's neon pair */\n"
        ":root {\n"
        f"  --accent: {primary};\n"
        f"  --accent-2: {secondary};\n"
        "}\n"
    )


def shell(project: dict, *, force: bool) -> dict:
    folder = Path(project["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "src"
    tor = src / "tor"
    tor.mkdir(parents=True, exist_ok=True)
    (folder / "public").mkdir(parents=True, exist_ok=True)

    written = []
    package_path = folder / "package.json"
    package = dict(PACKAGE)
    package["name"] = project["id"]
    package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")
    written.append(str(package_path))

    config = folder / "vite.config.ts"
    config.write_text(VITE_CONFIG.replace("%PORT%", str(project["port"])), encoding="utf-8")
    written.append(str(config))

    tsconfig = folder / "tsconfig.json"
    tsconfig.write_text(json.dumps(TSCONFIG, indent=2), encoding="utf-8")
    written.append(str(tsconfig))

    index = folder / "index.html"
    if force or not index.exists():
        index.write_text(
            "<!doctype html>\n<html lang=\"en\">\n  <head>\n"
            "    <meta charset=\"utf-8\" />\n"
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            f"    <meta name=\"description\" content=\"{project['name']} - {project['tagline']}\" />\n"
            "    <link rel=\"icon\" type=\"image/svg+xml\" href=\"/favicon.svg\" />\n"
            f"    <title>{project['name']}</title>\n  </head>\n  <body>\n"
            "    <div id=\"root\"></div>\n"
            "    <script type=\"module\" src=\"/src/main.tsx\"></script>\n"
            "  </body>\n</html>\n",
            encoding="utf-8",
        )
        written.append(str(index))

    for name in KIT_FILES:
        source = KIT / name
        if source.exists():
            shutil.copyfile(source, tor / name)
            written.append(str(tor / name))
    (tor / "accent.css").write_text(accent_css(project["id"]), encoding="utf-8")
    written.append(str(tor / "accent.css"))

    main = src / "main.tsx"
    if force or not main.exists():
        main.write_text(MAIN, encoding="utf-8")
        written.append(str(main))

    env = folder / ".env.example"
    env.write_text(
        "# point the app at another node (default: this machine's API)\n"
        "VITE_TOR_API=http://127.0.0.1:8090\n",
        encoding="utf-8",
    )
    written.append(str(env))
    return {"project": project["id"], "folder": str(folder), "files": len(written)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    rows = []
    for project in PROJECTS:
        if args.only and args.only != project["id"]:
            continue
        rows.append(shell(project, force=args.force))
    for row in rows:
        print(f"{row['project']:16} {row['files']:>2} files  {row['folder']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


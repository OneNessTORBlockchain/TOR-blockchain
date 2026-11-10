"""Generate the six platform logos (3D neon isometric SVG) into every folder.

    python tools/make_project_logos.py

Each logo is written three times: the node's ``images/projects`` (served at
``/assets/images/projects/<id>.svg``), the app's ``public/logo.svg`` and a 32px
favicon copy. Pure SVG, no binaries, no external assets — it renders in Tor
Browser with JavaScript disabled.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oneness_tor.services.projects import PROJECTS  # noqa: E402

#: id -> (primary, secondary, accent, glyph)
STYLE = {
    "tor-onion-swap": ("#00f0d0", "#0aa5ff", "#b8fff4", "swap"),
    "tor-screener": ("#35ff8a", "#00c2a8", "#d6ffe6", "screen"),
    "darkweb-crypto": ("#a45bff", "#ff2bd6", "#f0dcff", "cube"),
    "tor-pump": ("#ff7a3d", "#ff2f7a", "#ffe3cf", "pump"),
    "tor-charts": ("#4fa8ff", "#7b5cff", "#e2f0ff", "candles"),
    "solarix-engine": ("#ffc94a", "#ff7a2f", "#fff4d2", "sun"),
}

GLYPHS = {
    "swap": """
      <path d="M84 96h62l-14-15" fill="none" stroke="url(#neon)" stroke-width="11"
            stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M172 160H110l14 15" fill="none" stroke="url(#neon)" stroke-width="11"
            stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="84" cy="96" r="9" fill="url(#core)"/>
      <circle cx="172" cy="160" r="9" fill="url(#core)"/>
    """,
    "screen": """
      <rect x="70" y="70" width="116" height="86" rx="10" fill="none"
            stroke="url(#neon)" stroke-width="9"/>
      <path d="M88 138l22-26 18 14 30-38" fill="none" stroke="url(#neon)"
            stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M112 178h32" stroke="url(#neon)" stroke-width="9" stroke-linecap="round"/>
    """,
    "cube": """
      <path d="M128 62l58 32v66l-58 32-58-32V94z" fill="none" stroke="url(#neon)"
            stroke-width="9" stroke-linejoin="round"/>
      <path d="M70 94l58 32 58-32M128 126v66" fill="none" stroke="url(#neon)"
            stroke-width="7" opacity="0.8"/>
      <circle cx="128" cy="112" r="13" fill="url(#core)"/>
    """,
    "pump": """
      <path d="M96 166c0-38 16-70 32-92 16 22 32 54 32 92" fill="none"
            stroke="url(#neon)" stroke-width="10" stroke-linejoin="round"/>
      <path d="M112 168h32l-16 28z" fill="url(#core)"/>
      <path d="M78 132l-14 12 20 4M178 132l14 12-20 4" fill="none"
            stroke="url(#neon)" stroke-width="8" stroke-linecap="round"/>
    """,
    "candles": """
      <rect x="86" y="104" width="16" height="54" rx="4" fill="url(#neon)"/>
      <rect x="120" y="78" width="16" height="82" rx="4" fill="url(#neon)"/>
      <rect x="154" y="126" width="16" height="44" rx="4" fill="url(#core)"/>
      <path d="M94 88v16M94 158v14M128 62v16M128 160v14M162 110v16"
            stroke="url(#core)" stroke-width="6" stroke-linecap="round"/>
    """,
    "sun": """
      <circle cx="128" cy="128" r="38" fill="none" stroke="url(#neon)" stroke-width="10"/>
      <circle cx="128" cy="128" r="13" fill="url(#core)"/>
      <g stroke="url(#neon)" stroke-width="8" stroke-linecap="round">
        <path d="M128 62v20M128 174v20M62 128h20M174 128h20"/>
        <path d="M81 81l14 14M161 161l14 14M175 81l-14 14M95 161l-14 14"/>
      </g>
    """,
}


def logo(project_id: str) -> str:
    primary, secondary, accent, glyph = STYLE[project_id]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256" role="img" aria-label="TOR {glyph} logo">
  <defs>
    <linearGradient id="neon" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{primary}"/>
      <stop offset="0.55" stop-color="{secondary}"/>
      <stop offset="1" stop-color="{accent}"/>
    </linearGradient>
    <radialGradient id="core" cx="0.5" cy="0.45" r="0.6">
      <stop offset="0" stop-color="{accent}"/>
      <stop offset="1" stop-color="{secondary}"/>
    </radialGradient>
    <linearGradient id="plate" x1="0" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="#0b1220"/>
      <stop offset="0.6" stop-color="#060a12"/>
      <stop offset="1" stop-color="{primary}" stop-opacity="0.18"/>
    </linearGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{accent}" stop-opacity="0.9"/>
      <stop offset="1" stop-color="{primary}" stop-opacity="0.05"/>
    </linearGradient>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="7" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <pattern id="grid" width="16" height="16" patternUnits="userSpaceOnUse">
      <path d="M16 0H0v16" fill="none" stroke="{primary}" stroke-opacity="0.10" stroke-width="1"/>
    </pattern>
  </defs>
  <rect x="4" y="4" width="248" height="248" rx="52" fill="url(#plate)"/>
  <rect x="4" y="4" width="248" height="248" rx="52" fill="url(#grid)"/>
  <path d="M28 196l100-58 100 58-100 58z" fill="{primary}" opacity="0.08"/>
  <path d="M28 60l100-58 100 58-100 58z" fill="{secondary}" opacity="0.06"/>
  <rect x="4" y="4" width="248" height="248" rx="52" fill="none" stroke="url(#edge)" stroke-width="2.5"/>
  <g filter="url(#glow)" stroke-linejoin="round">{GLYPHS[glyph]}</g>
  <text x="128" y="232" text-anchor="middle" font-family="Inter,Segoe UI,Arial,sans-serif"
        font-size="15" letter-spacing="4" fill="{accent}" fill-opacity="0.85">TOR</text>
</svg>
"""


def write_all() -> list[dict]:
    written = []
    node_dir = ROOT / "images" / "projects"
    node_dir.mkdir(parents=True, exist_ok=True)
    for project in PROJECTS:
        body = logo(project["id"])
        folder = Path(project["folder"])
        targets = [node_dir / f"{project['id']}.svg"]
        if folder.exists():
            public = folder / "public"
            public.mkdir(parents=True, exist_ok=True)
            targets += [public / "logo.svg", public / "favicon.svg"]
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            written.append({"project": project["id"], "path": str(target),
                            "bytes": len(body)})
    return written


if __name__ == "__main__":
    for row in write_all():
        print(f"{row['project']:16} {row['bytes']:>6} B  {row['path']}")


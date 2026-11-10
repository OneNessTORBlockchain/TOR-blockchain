"""Every HTML page: landing, dashboard, explorer, wallet, payments, contracts,
app builders, Ollama console, docs, the TOR coin page and the generated
landing pages served at ``/sites/<id>/``."""
from __future__ import annotations

import mimetypes

from flask import (Blueprint, Response, abort, redirect, render_template,
                   send_from_directory, url_for)

from core import coin
from core.config import ROOT, settings
from core.errors import NotFound, ValidationError
from oneness_tor.services import (builder, live_snapshot, token_media,
                                  workbench)

bp = Blueprint("pages", __name__)

#: Token logos are mirrored from market data, so the extensions vary (webp is
#: common and some Windows/Python builds do not know it). Registering the types
#: makes ``/assets/tokens/…`` answer ``image/*`` instead of ``octet-stream``.
for _type, _suffixes in {"image/webp": (".webp",), "image/avif": (".avif",),
                         "image/svg+xml": (".svg",)}.items():
    for _suffix in _suffixes:
        mimetypes.add_type(_type, _suffix)

NAV = [
    {"slug": "dashboard", "label": "Command deck", "glyph": "◈",
     "hint": "ask the node, watch the real API calls"},
    {"slug": "explorer", "label": "Explorer", "glyph": "⛓",
     "hint": "blocks, records, accounts"},
    {"slug": "admin", "label": "Node admin", "glyph": "⚙",
     "hint": "sync, verify, archive, deploy rights"},
    {"slug": "wallet", "label": "Wallet", "glyph": "⚿",
     "hint": "balances, send, sign"},
    {"slug": "payments", "label": "Payments", "glyph": "◉",
     "hint": "invoices & revenue"},
    {"slug": "contracts", "label": "Contracts", "glyph": "⌬",
     "hint": "Rust deploys, 5 TOR"},
    {"slug": "workbench", "label": "App builders", "glyph": "⌗",
     "hint": "DarkWeb + Rust: files, run, preview"},
    {"slug": "meme", "label": "Meme launcher", "glyph": "🚀",
     "hint": "coins, pools, swaps, DEX seams"},
    {"slug": "nft", "label": "AI-NFT Studio", "glyph": "🖼",
     "hint": "mint 3 TOR, speak as the token"},
    {"slug": "ollama", "label": "Ollama", "glyph": "◍",
     "hint": "models, pull, run"},
    {"slug": "docs", "label": "API docs", "glyph": "❰❱",
     "hint": "every endpoint, live"},
    {"slug": "coin", "label": "TOR coin", "glyph": "⬡",
     "hint": "metadata & logo"},
]


def _context(page: str) -> dict:
    from oneness_tor.services import accounts, privacy

    return {"page": page, "nav": NAV, "coin": coin.metadata(),
            # the wallet asks before every transaction (oneness-tor/privacy/1)
            "privacy_label": privacy.label(),
            "privacy_default_mode": privacy.default_mode(),
            "support_email": accounts.support_email(),
            "node_url": str(settings.get("node.api_url")),
            "ollama_url": str(settings.get("ollama.base_url")),
            # Printed into the HTML so the numbers are on screen even when the
            # browser blocks JavaScript (Tor Browser Safer/Safest on http://).
            "live": live_snapshot.snapshot()}


def _page(template: str, page: str) -> str:
    return render_template(template, **_context(page))


@bp.get("/")
def index():
    return _page("index.html", "home")


@bp.get("/dashboard")
def dashboard():
    return _page("dashboard.html", "dashboard")


@bp.get("/explorer")
def explorer():
    return _page("explorer.html", "explorer")


@bp.get("/admin")
def admin_page():
    return _page("admin.html", "admin")


@bp.get("/wallet")
def wallet_page():
    return _page("wallet.html", "wallet")


@bp.get("/payments")
def payments_page():
    return _page("payments.html", "payments")


@bp.get("/contracts")
def contracts_page():
    return _page("contracts.html", "contracts")


@bp.get("/workbench")
def workbench_page():
    return _page("workbench.html", "workbench")


@bp.get("/meme")
def meme_page():
    return _page("meme.html", "meme")


@bp.get("/nft")
def nft_page():
    return _page("nft.html", "nft")


@bp.get("/ollama")
def ollama_page():
    return _page("ollama.html", "ollama")


@bp.get("/docs")
def docs_page():
    return _page("docs.html", "docs")


@bp.get("/coin")
def coin_page():
    return _page("coin.html", "coin")


@bp.get("/sites/<site_id>/")
def hosted_site(site_id: str):
    """Serve a generated AI token landing page at its public URL."""
    try:
        html = builder.html_of(site_id)
    except NotFound:
        abort(404)
    try:
        builder.record_view(site_id)
    except NotFound:
        pass
    response = Response(html, mimetype="text/html")
    response.headers["x-oneness-site"] = site_id
    return response


@bp.get("/sites/<site_id>")
def hosted_site_bare(site_id: str):
    return redirect(url_for("pages.hosted_site", site_id=site_id))


@bp.get("/builds/<build_id>/")
def build_preview(build_id: str):
    """The preview window: the project's entry file, straight off disk."""
    return _build_asset(build_id, "")


@bp.get("/builds/<build_id>/<path:asset>")
def build_asset(build_id: str, asset: str):
    return _build_asset(build_id, asset)


def _build_asset(build_id: str, asset: str):
    """Serve a workbench file with its real mimetype and no caching."""
    try:
        body, mime = workbench.preview_file(build_id, asset)
    except NotFound:
        abort(404)
    except ValidationError as problem:
        abort(400, problem.message)
    response = Response(body, mimetype=mime)
    response.headers["x-oneness-build"] = build_id
    response.headers["cache-control"] = "no-store"
    return response


@bp.get("/assets/images/<path:name>")
def image(name: str):
    folder = ROOT / str(settings.get("paths.images_dir") or "images")
    if not (folder / name).is_file():
        abort(404)
    return send_from_directory(folder, name, max_age=3600)


@bp.get("/assets/tokens/<path:name>")
def token_logo(name: str):
    """A token logo mirrored on this node — no third party, no hotlink, no Tor exit.

    ``token_media`` copies every picture it resolves into
    ``data/metadata/token-logos`` (see ``POST /api/tokens/images/mirror``), so a
    token row draws a local file whether or not CoinGecko is reachable from the
    browser that is looking at the page.
    """
    folder = token_media.logo_dir()
    if not (folder / name).is_file():
        abort(404)
    return send_from_directory(folder, name, max_age=86400)

"""Single-file page generator — real generation, real files, real serving.

This is the engine behind the meme launcher's paid **AI token landing page**
(``POST /api/meme/tokens/{symbol}/landing``): the prompt is charged (10 TOR)
before generation, the model output must contain a complete HTML document
(otherwise the call fails loudly), and the file is served at ``/sites/<id>/`` so
the result can be opened immediately. It has no page or endpoint of its own.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from core import ollama
from core.config import settings
from core.errors import NotFound, ValidationError
from core.validator import format_tor
from oneness_tor.services.store import Collection, data_dir

SITES = Collection(lambda: data_dir("sites") / "sites.json", "site")

#: Output budgets tried for the first ask and one bigger retry: a full page on a
#: chatty model can run past 4k tokens, and a page that stops before ``</html>``
#: is worthless (it used to be written to disk anyway).
BUILD_BUDGETS = (4096, 16384)

#: The AI surface this builder draws its model from (see ``core.ollama.routing``).
SURFACE = "web_builder"

BUILD_SYSTEM = """You are a senior front-end engineer building a single-file
website for the OneNess TOR network. Hard requirements:
- Output ONLY a complete HTML5 document: <!DOCTYPE html> … </html>. No prose,
  no markdown fences, no explanations.
- Everything inline: CSS in <style>, JS in <script>, images as inline SVG or CSS
  gradients (the site will be served from a Tor hidden service with no CDN).
- Dark neon / holographic aesthetic: deep blacks, cyan/magenta glow, glass
  panels, 3D transforms, subtle animation. Responsive, accessible, fast.
- Real content for the requested topic: headline, sections, call to action,
  footer mentioning OneNess TOR.
- No external requests of any kind (no fonts, scripts, images, analytics).
- Stay lean: the whole document must fit in about 16 KB (roughly 6000 tokens).
  Compact CSS, one or two small inline SVGs, no repeated boilerplate — a page
  that runs past the output budget is cut off and thrown away."""


def _extract_html(text: str) -> str:
    body = re.sub(r"```(?:html)?", "", str(text or "")).strip("` \n")
    match = re.search(r"<!DOCTYPE html|<html", body, re.I)
    if not match:
        raise ValidationError(
            "the model did not return an HTML document — try another model or a "
            "clearer prompt", details={"preview": body[:300]})
    body = body[match.start():]
    end = body.lower().rfind("</html>")
    if end == -1:
        raise ValidationError(
            "the model's HTML document was cut off before </html> — raise the "
            "token budget or ask for a smaller page",
            details={"preview": body[:300], "truncated": True})
    return body[:end + len("</html>")]


def generate(prompt: str, *, title: str = "", model: str | None = None,
             existing: str = "", extra: str = "") -> dict:
    """Ask Ollama for a full page; returns {html, model, bytes}."""
    text = str(prompt or "").strip()
    if not text:
        raise ValidationError("prompt is required")
    chosen = ollama.pick_model(model, purpose=SURFACE)
    user = f"Build the website: {text}\n"
    if title:
        user += f"Use '{title}' as the site name in the header and <title>.\n"
    if existing:
        user += ("Here is the current HTML. Return the complete updated document.\n"
                 f"<current>\n{existing[:12000]}\n</current>\n")
    if extra:
        user += extra + "\n"
    user += "Return only the HTML document."
    think = False if ollama.supports_thinking(chosen) else None
    reply: dict = {}
    html, problem = "", None
    for budget in BUILD_BUDGETS:
        reply = ollama.generate(chosen, user, system=BUILD_SYSTEM,
                                options={"temperature": 0.7,
                                         "num_predict": budget},
                                think=think)
        try:
            html = _extract_html(reply.get("response", ""))
        except ValidationError as error:
            problem, html = error, ""
        if html or str(reply.get("done_reason") or "") != "length":
            break
    if not html:
        thinking = len(str(reply.get("thinking") or ""))
        if thinking and not str(reply.get("response") or "").strip():
            raise ValidationError(
                "the model spent every token thinking and never wrote the page "
                "— pick a non-reasoning model or raise the token budget",
                details={"model": chosen, "thinking_chars": thinking})
        raise problem if problem else ValidationError(
            "the model did not return an HTML document — try another model or a "
            "clearer prompt", details={"model": chosen})
    return {"html": html, "model": chosen, "prompt": text,
            "bytes": len(html.encode("utf-8")),
            "eval_count": reply.get("eval_count")}


def site_dir(site_id: str):
    return data_dir("sites", str(site_id))


def write_html(site_id: str, html: str) -> str:
    path = site_dir(site_id) / "index.html"
    path.write_text(html, encoding="utf-8")
    return str(path)


def html_of(site_id: str) -> str:
    path = site_dir(site_id) / "index.html"
    if not path.exists():
        raise NotFound(f"site {site_id} has no HTML file")
    return path.read_text(encoding="utf-8")


def preview_url(site_id: str) -> str:
    return f"{str(settings.get('paths.public_base_url')).rstrip('/')}/sites/{site_id}/"


def create(prompt: str, html: str, *, model: str, wallet: str,
           txid: str | None, title: str = "", charge: dict | None = None) -> dict:
    site = SITES.add({
        "title": (title or prompt)[:80].strip() or "Untitled",
        "prompt": prompt, "model": model, "wallet": wallet, "txid": txid,
        "bytes": len(html.encode("utf-8")),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_unix": int(time.time()), "views": 0,
        "charge": {"tor": charge.get("amount_tor") if charge else None,
                   "micro": charge.get("amount_micro") if charge else None}})
    write_html(site["id"], html)
    site["preview_url"] = preview_url(site["id"])
    return site


def list_sites(limit: int = 50) -> dict:
    rows = SITES.all()
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    rows = rows[:max(1, int(limit))]
    for row in rows:
        row["preview_url"] = preview_url(str(row.get("id")))
    return {"sites": rows, "count": len(SITES.all())}


def get_site(site_id: str, *, with_html: bool = True) -> dict:
    site = SITES.get(site_id)
    site["preview_url"] = preview_url(str(site.get("id")))
    if with_html:
        try:
            site["html"] = html_of(site_id)
        except NotFound:
            site["html"] = ""
    return site


def revise(site_id: str, prompt: str, *, model: str | None = None) -> dict:
    site = SITES.get(site_id)
    generated = generate(prompt, title=str(site.get("title") or ""),
                         model=model, existing=html_of(site_id))
    write_html(site_id, generated["html"])
    updated = SITES.update(site_id, bytes=generated["bytes"],
                           model=generated["model"],
                           revised_at=datetime.now(timezone.utc).isoformat(
                               timespec="seconds"),
                           revision_count=int(site.get("revision_count") or 0) + 1)
    updated["preview_url"] = preview_url(site_id)
    return {"site": updated, "model": generated["model"],
            "bytes": generated["bytes"]}


def record_view(site_id: str) -> dict:
    site = SITES.get(site_id)
    return SITES.update(site_id, views=int(site.get("views") or 0) + 1)

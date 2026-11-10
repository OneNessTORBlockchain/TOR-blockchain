"""Endpoint audit — prove the catalog, the routes, the tool and the prompt agree.

    python tools/audit_endpoints.py            # static checks + live probes
    python tools/audit_endpoints.py --static   # no HTTP calls

Static checks (always run):

* every ``/api`` route in the Flask url map exists in ``core.api_catalog`` and
  every catalog entry maps to a real route (method + path, converters folded),
* ``api_catalog.prompt_text`` lists every endpoint **and its return shape**, so
  the Ollama system prompt cannot describe an endpoint that does not exist,
* the ``fetch_oneness_data`` tool enum and the OpenAPI document cover everything,
* which pages reference which endpoint (unreferenced ones are reported, never fatal).

Live checks (skipped with ``--static``) call **every GET endpoint through
:class:`core.tools.ToolBox`** — the same code path the Ollama brain uses — and
report the real HTTP status. 2xx = PASS, an answered 4xx/501 = SOFT (the endpoint
spoke honestly), 0/5xx = FAIL. Exit code 1 on drift or a FAIL.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import api_catalog  # noqa: E402
from core.config import settings  # noqa: E402
from core.tools import ToolBox  # noqa: E402

CONVERTER = re.compile(r"<(?:[^:>]+:)?([^>]+)>")
WEB = ROOT / "oneness_tor" / "web"


def normalize(path: str) -> str:
    """``/api/chain/block/<ref>`` → ``/api/chain/block/{ref}``."""
    return CONVERTER.sub(lambda match: "{" + match.group(1) + "}", path)


# ------------------------------------------------------------------ static
def flask_routes() -> set[tuple[str, str]]:
    from oneness_tor.app import create_app

    routes: set[tuple[str, str]] = set()
    for rule in create_app().url_map.iter_rules():
        path = normalize(str(rule.rule))
        if not path.startswith("/api"):
            continue
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            routes.add((method, path))
    return routes


def catalog_routes() -> set[tuple[str, str]]:
    return {(entry["method"], normalize(entry["path"])) for entry in api_catalog.ALL}


def page_coverage() -> dict[str, int]:
    """How many page/JS files mention each endpoint's path prefix."""
    blob = "".join(path.read_text(encoding="utf-8")
                   for path in WEB.rglob("*")
                   if path.suffix in (".html", ".js"))
    return {entry["name"]: blob.count(entry["path"].split("{")[0].rstrip("/"))
            for entry in api_catalog.ALL}


def check_static(errors: list[str], warnings: list[str]) -> None:
    routes, catalog = flask_routes(), catalog_routes()
    for method, path in sorted(routes - catalog):
        errors.append(f"route not in the catalog: {method} {path}")
    for method, path in sorted(catalog - routes):
        errors.append(f"catalog entry has no route: {method} {path}")

    prompt = api_catalog.prompt_text(str(settings.get("paths.public_base_url")))
    for entry in api_catalog.ALL:
        if entry["name"] not in prompt:
            errors.append(f"system prompt omits {entry['name']}")
        if entry["returns"] and entry["returns"] not in prompt:
            errors.append(f"system prompt omits the return shape of {entry['name']}")

    enum = api_catalog.tool_spec()["function"]["parameters"]["properties"][
        "endpoint"]["enum"]
    if enum != api_catalog.names():
        errors.append("fetch_oneness_data enum does not match the registry")

    operations = sum(len(ops) for ops in
                     api_catalog.openapi("http://x")["paths"].values())
    if operations != len(api_catalog.ALL):
        errors.append(f"OpenAPI has {operations} operations, registry has "
                      f"{len(api_catalog.ALL)}")

    for name, hits in sorted(page_coverage().items()):
        if not hits:
            warnings.append(f"no page calls {name}")


# -------------------------------------------------------------------- live
def walk(blob):
    if isinstance(blob, dict):
        yield blob
        for value in blob.values():
            yield from walk(value)
    elif isinstance(blob, list):
        for value in blob:
            yield from walk(value)


def first_value(result, *keys) -> str | None:
    """First scalar found under any of ``keys`` anywhere in the response."""
    for obj in walk(result.get("data")):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value)
    return None


def build_context(box) -> dict:
    """Real ids read from the live node — never invented samples."""
    def call(name, params=None):
        try:
            return box.execute(name, params)
        except Exception:  # a missing sample must not stop the audit
            return {"data": None}

    info = call("chain.info").get("data") or {}
    height = int(info.get("height") or 1)
    return {
        "address": info.get("founder"),
        "ref": max(0, height - 1),
        "txid": first_value(call("chain.events", {"limit": 5}), "txid"),
        "name": first_value(call("wallets.list"), "name"),
        "invoice_id": first_value(call("payments.invoices.list", {"limit": 5}),
                                  "invoice_id", "id"),
        "contract_id": first_value(call("contracts.list"), "contract_id", "id"),
        "site_id": first_value(call("builder.sites"), "site_id", "id"),
        "build_id": first_value(call("workbench.projects", {"limit": 5}),
                                "build_id", "id"),
        "path": "index.html",
        "symbol": first_value(call("meme.tokens", {"limit": 5}), "symbol"),
        "nft_id": first_value(call("nft.catalogue", {"limit": 5}), "nft_id", "id"),
        "code": first_value(call("payments.plans"), "code") or "contract_create",
        "amount": 1,
        "url": "https://example.com",
        "model": "llama3.2:3b",
    }


def params_for(entry: dict, ctx: dict) -> tuple[dict, str | None]:
    params: dict = {}
    for param in entry["params"]:
        value = ctx.get(param["name"])
        if value in (None, ""):
            value = param.get("example")
        if value in (None, ""):
            if param["required"]:
                return params, f"no sample for {param['name']!r}"
            continue
        params[param["name"]] = value
    return params, None


#: Documented placeholders answer 501/404 on purpose — honest seams, not failures.
HONEST_SEAMS = {"DEX_NOT_DEPLOYED", "ENGINE_NOT_DEPLOYED", "NFT_NOT_FOUND"}


def live_probe(errors: list[str], warnings: list[str]) -> list[tuple]:
    """Call every GET endpoint *through the tool* and report the real status."""
    base = str(settings.get("paths.public_base_url"))
    box = ToolBox(base, admin_token=settings.admin_token, timeout=30)
    rows: list[tuple] = []
    if not box.execute("chain.health").get("ok"):
        errors.append(f"{base} is not answering — start `python -m oneness_tor`")
        return rows
    ctx = build_context(box)
    for entry in api_catalog.ALL:
        if entry["method"] != "GET":
            rows.append((entry["name"], "SKIP", 0, "write endpoint - not probed"))
            continue
        params, problem = params_for(entry, ctx)
        if problem:
            rows.append((entry["name"], "SKIP", 0, problem))
            warnings.append(f"{entry['name']}: {problem}")
            continue
        result = box.execute(entry["name"], params)
        status = int(result.get("http_status") or 0)
        code = (result.get("error") or {}).get("code") or ""
        latency = result.get("latency_ms") or 0
        if status == 0:
            verdict = "FAIL"
            errors.append(f"{entry['name']} -> unreachable: "
                          f"{(result.get('error') or {}).get('message', '')}")
        elif code in HONEST_SEAMS:
            verdict = "SOFT"  # "not deployed yet" is an honest answer, not a crash
        elif 200 <= status < 300:
            verdict = "PASS"  # raw documents (OpenAPI) carry no envelope
        elif status >= 500:
            verdict = "FAIL"
            errors.append(f"{entry['name']} -> {status} {code}: "
                          f"{(result.get('error') or {}).get('message', '')}")
        else:
            verdict = "SOFT"
        rows.append((entry["name"], verdict, status, f"{code} {latency}ms".strip()))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="audit the endpoint catalog")
    parser.add_argument("--static", action="store_true",
                        help="static checks only, no HTTP calls")
    args = parser.parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    print(f"catalog v{api_catalog.CATALOG_VERSION}: {len(api_catalog.ALL)} endpoints")
    check_static(errors, warnings)
    print(f"static: {len(errors)} error(s), {len(warnings)} warning(s)")

    if not args.static:
        rows = live_probe(errors, warnings)
        if rows:
            print("\nlive probes through the fetch-data tool:")
            for name, verdict, status, note in rows:
                print(f"  {verdict:4} {name:26} {status or '-':>4}  {note}")
            counts: dict[str, int] = {}
            for _, verdict, _, _ in rows:
                counts[verdict] = counts.get(verdict, 0) + 1
            print("\n" + " | ".join(f"{key} {value}"
                                    for key, value in sorted(counts.items())))

    for warning in warnings:
        print(f"  warn  {warning}")
    if errors:
        print("\nFAILED:")
        for problem in errors:
            print("  -", problem)
        return 1
    print("\nall endpoint checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

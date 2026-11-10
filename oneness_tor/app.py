"""OneNess TOR node application (API + dashboard, one Flask process).

    python -m oneness_tor                 # http://127.0.0.1:8090
    python -m oneness_tor --port 9000

The Rust node (``torchain serve``) must be running on ``node.api_url`` for chain
data; every page and the Ollama tools read the ledger through it.
"""
from __future__ import annotations

import gzip
import logging

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from core.config import settings
from core.envelope import fail
from core.errors import AppError
from oneness_tor.api.accounts import bp as accounts_bp
from oneness_tor.api.brain import bp as brain_bp
from oneness_tor.api.chain import bp as chain_bp
from oneness_tor.api.dev import bp as dev_bp
from oneness_tor.api.fees import bp as fees_bp
from oneness_tor.api.ipfs import bp as ipfs_bp
from oneness_tor.api.market import bp as market_bp
from oneness_tor.api.meme import bp as meme_bp
from oneness_tor.api.nft import bp as nft_bp
from oneness_tor.api.pages import bp as pages_bp
from oneness_tor.api.public import bp as public_bp
from oneness_tor.api.services import bp as services_bp
from oneness_tor.api.wallet import bp as wallet_bp
from oneness_tor.api.workbench import bp as workbench_bp
from oneness_tor.services import base_url

__version__ = "1.0.0"
BLUEPRINTS = (chain_bp, wallet_bp, accounts_bp, services_bp, meme_bp, nft_bp,
              brain_bp, workbench_bp, public_bp, ipfs_bp, dev_bp, market_bp,
              pages_bp, fees_bp)

#: Always open even when ``api.require_key`` is on: sign-in, the catalog, the
#: key shop itself and the keyed gateway (which checks its own key).
KEY_EXEMPT = ("/api/auth/", "/api/admin/", "/api/dev/keys/pricing",
              "/api/v1/", "/api/docs", "/api/openapi.json", "/api/health",
              "/api/privacy", "/api/coin", "/api/pricing")

#: Bodies above this size are gzipped when the client asks for it. A Tor hidden
#: service is slow and a mobile Tor circuit drops long single-shot streams: the
#: ``/api/docs`` catalog is ~99 KB of JSON, which arrives as a closed connection
#: ("Empty reply from server" / "network error") instead of a payload. Gzipped
#: it is ~7 KB, so the same request finishes in one stream segment.
GZIP_MIN_BYTES = 1024
GZIP_MIMETYPES = ("application/json", "text/html", "text/plain", "text/css",
                  "text/javascript", "application/javascript", "image/svg+xml")


def _gzip_if_accepted(response):
    """Compress a text/JSON body for clients that advertise ``gzip``.

    Only runs when the client sends ``Accept-Encoding: gzip``, so Werkzeug's
    test client (which sends no such header) keeps receiving plain bodies and
    ``get_json()`` keeps working.
    """
    if (request.method == "HEAD" or response.direct_passthrough
            or response.status_code != 200
            or response.headers.get("Content-Encoding")):
        return response
    if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
        return response
    if (response.mimetype or "").lower() not in GZIP_MIMETYPES:
        return response
    body = response.get_data()
    if len(body) < GZIP_MIN_BYTES:
        return response
    response.set_data(gzip.compress(body, 6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.get_data()))
    response.headers["Vary"] = "Accept-Encoding"
    response.headers.pop("ETag", None)     # validators no longer describe the body
    return response



def create_app(**overrides) -> Flask:
    app = Flask(__name__, static_folder="web/static",
                template_folder="web/templates", static_url_path="/static")
    app.json.sort_keys = False
    app.config.update(MAX_CONTENT_LENGTH=4 * 1024 * 1024,
                      JSON_AS_ASCII=False,
                      TEMPLATES_AUTO_RELOAD=True,
                      ONENESS_VERSION=__version__)
    app.config.update(overrides)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)

    @app.after_request
    def _headers(response):
        path = request.path
        if path.startswith("/api") or path == "/health":
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Headers"] = (
                "content-type,x-admin-token,authorization,x-admin-session,"
                "x-wallet-session,x-api-key")
            response.headers["Access-Control-Allow-Methods"] = (
                "GET,POST,DELETE,OPTIONS")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not path.startswith(("/sites/", "/builds/")):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self' 'unsafe-inline' data: blob:; "
                "img-src 'self' data: https:; "
                "frame-src 'self' http://127.0.0.1:* http://localhost:*")
        return _gzip_if_accepted(response)

    @app.before_request
    def _api_key_gate():
        """Optional API-key enforcement for a headless deployment.

        ``api.require_key: true`` (``ONENESS_REQUIRE_API_KEY=1``) makes every
        ``/api`` call carry an x-api-key, an admin credential or a wallet
        session. It is OFF by default because the website calls the same API
        from the browser; the versioned developer surface ``/api/v1`` always
        requires a key on its own.
        """
        if request.method == "OPTIONS" or not settings.get("api.require_key"):
            return None
        path = request.path
        if not path.startswith("/api") or path.startswith(KEY_EXEMPT):
            return None
        from core.errors import Forbidden
        from oneness_tor.services import developer_keys, wallet_access
        from oneness_tor.services.auth import is_admin_request

        if is_admin_request(request) or wallet_access.session(request):
            return None
        presented = (request.headers.get(developer_keys.HEADER)
                     or request.args.get("api_key") or "").strip()
        if presented and developer_keys.find(presented):
            return None           # the endpoint itself validates expiry/limits
        raise Forbidden(
            "this API requires a developer key: buy one at /docs and send it "
            "as x-api-key (admin credentials and wallet sessions also work)",
            details={"header": developer_keys.HEADER,
                     "pricing": "GET /api/dev/keys/pricing"})


    @app.before_request
    def _remember_public_host():
        """Learn this node's own public host from the request it just received.

        A custom domain (``https://www.torblockchain.fun``) in front of a Render
        service is invisible in the environment, which advertises only
        ``https://<service>.onrender.com``. Without it the node cannot see that
        ``origin.url`` — its own address — is *itself*, so it demotes itself to a
        read-only replica of itself: every mint/send answers ``409
        SYNC_READ_ONLY`` while the chain looks perfectly healthy. Runs before the
        view, so the very first request that arrives on the custom domain is
        already enough for it to know.
        """
        base_url.remember(request)
        return None

    @app.before_request
    def _preflight():
        if request.method == "OPTIONS":
            return ("", 204)

    @app.errorhandler(AppError)
    def _app_error(error: AppError):
        return jsonify(fail(error)), error.http_status

    @app.errorhandler(HTTPException)
    def _http_error(error: HTTPException):
        if request.path.startswith("/api") or request.path == "/health":
            wrapped = AppError(error.description or error.name,
                               code=error.name.upper().replace(" ", "_"),
                               http_status=error.code or 500)
            return jsonify(fail(wrapped)), error.code or 500
        return error

    @app.errorhandler(Exception)
    def _unexpected(error: Exception):
        logging.getLogger("oneness_tor").exception("unhandled error")
        if request.path.startswith("/api") or request.path == "/health":
            return jsonify(fail(AppError("internal error"))), 500
        return ("internal error", 500)

    return app


app = create_app()

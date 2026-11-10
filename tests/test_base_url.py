"""The docs must advertise the URL the visitor actually reached (no guessing)."""
from __future__ import annotations

from types import SimpleNamespace

from oneness_tor.services import base_url


def request(**headers) -> SimpleNamespace:
    scheme, host = headers.pop("_scheme", "http"), headers.pop("_host", "127.0.0.1:8090")
    return SimpleNamespace(headers={key.replace("_", "-"): value
                                    for key, value in headers.items()},
                           scheme=scheme, host=host,
                           host_url=f"{scheme}://{host}/")


def test_normalize_strips_and_forces_http_for_onions(monkeypatch):
    monkeypatch.delenv("ONENESS_PUBLIC_BASE_URL", raising=False)
    assert base_url.normalize("http://127.0.0.1:8090/") == "http://127.0.0.1:8090"
    assert base_url.normalize("oneness.onrender.com") == "http://oneness.onrender.com"
    assert base_url.normalize("https://abc.onion/docs/") == "http://abc.onion/docs"
    assert base_url.normalize("") == ""
    assert base_url.normalize("user:secret@host:80") == "http://host:80"


def test_render_forwarded_headers_win_over_the_internal_host(monkeypatch):
    monkeypatch.delenv("ONENESS_PUBLIC_BASE_URL", raising=False)
    seen = request(x_forwarded_proto="https", x_forwarded_host="oneness.onrender.com")
    assert base_url.request_base(seen) == "https://oneness.onrender.com"

    # an explicit port survives, the default one does not
    assert base_url.request_base(request(
        x_forwarded_proto="https", x_forwarded_host="box.example",
        x_forwarded_port="443")) == "https://box.example"
    assert base_url.request_base(request(
        x_forwarded_proto="http", x_forwarded_host="box.example",
        x_forwarded_port="8099")) == "http://box.example:8099"


def test_rfc7239_forwarded_header_is_understood(monkeypatch):
    monkeypatch.delenv("ONENESS_PUBLIC_BASE_URL", raising=False)
    seen = request(forwarded='for=1.2.3.4;proto=https;host="tor.example"')
    assert base_url.request_base(seen) == "https://tor.example"


def test_onion_host_is_always_plain_http(monkeypatch):
    monkeypatch.delenv("ONENESS_PUBLIC_BASE_URL", raising=False)
    seen = request(_scheme="https", _host="abcd1234.onion")
    assert base_url.request_base(seen) == "http://abcd1234.onion"


def test_pinned_env_wins_and_is_reported_as_the_source(monkeypatch):
    monkeypatch.setenv("ONENESS_PUBLIC_BASE_URL", "https://tor.onenesstech.ca/")
    urls = base_url.base_urls(request(x_forwarded_host="internal:10000"))
    assert urls["current"] == "https://tor.onenesstech.ca"
    assert urls["configured"] == "https://tor.onenesstech.ca"
    assert "ONENESS_PUBLIC_BASE_URL" in urls["source"]
    assert urls["request"] == "http://127.0.0.1:8090"


def test_base_urls_explains_where_the_url_came_from(monkeypatch):
    monkeypatch.delenv("ONENESS_PUBLIC_BASE_URL", raising=False)
    urls = base_url.base_urls(request(x_forwarded_proto="https",
                                      x_forwarded_host="oneness.onrender.com"))
    assert urls["current"] == "https://oneness.onrender.com"
    assert "X-Forwarded" in urls["source"]
    assert urls["configured"] is None
    assert urls["listen"].startswith("http://")


def test_internal_base_is_loopback_for_an_onion_visitor(monkeypatch):
    """In-process tool calls must never dial the onion the browser used.

    ``<id>.onion`` only resolves inside Tor; the server has no SOCKS proxy, so a
    self-call to it fails with ``TOOL_UNREACHABLE`` (getaddrinfo failed).
    """
    monkeypatch.delenv("ONENESS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ONENESS_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("ONENESS_API_PORT", "8090")

    seen = request(_host="abcd1234.onion")
    assert base_url.request_base(seen) == "http://abcd1234.onion"
    assert base_url.internal_base() == "http://127.0.0.1:8090"
    assert ".onion" not in base_url.internal_base()

    monkeypatch.setenv("ONENESS_INTERNAL_BASE_URL", "http://127.0.0.1:9999/")
    assert base_url.internal_base() == "http://127.0.0.1:9999"


# ------------------------------------- the hosts this node actually answers for
def test_a_public_host_is_remembered_once(tmp_path, monkeypatch):
    """The custom domain a Render service answers on is nowhere in the env.

    It is in the requests, though, and that is how the origin finds out that
    ``origin.url`` is *itself* instead of demoting itself to a replica of itself.
    """
    path = tmp_path / "self_hosts.json"
    monkeypatch.setattr(base_url, "REMEMBERED_PATH", path)

    seen = request(x_forwarded_proto="https", x_forwarded_host="www.torblockchain.fun")
    assert base_url.remember(seen) == "www.torblockchain.fun"
    assert "www.torblockchain.fun" in base_url.known_hosts()

    first = path.stat().st_mtime_ns
    assert base_url.remember(seen) == "www.torblockchain.fun"   # already known
    assert path.stat().st_mtime_ns == first                      # no rewrite per hit

    assert base_url.remember(request(x_forwarded_host="onenesstorchain.onrender.com"))
    assert sorted(base_url.known_hosts()) == ["onenesstorchain.onrender.com",
                                             "www.torblockchain.fun"]


def test_loopback_and_bare_words_are_never_remembered(tmp_path, monkeypatch):
    """``localhost:8090`` is this machine talking to itself, not a public identity."""
    path = tmp_path / "self_hosts.json"
    monkeypatch.setattr(base_url, "REMEMBERED_PATH", path)
    assert base_url.remember(request(_host="127.0.0.1:8090")) == ""
    assert base_url.remember(request(_host="localhost:8090")) == ""
    assert base_url.remember(request(_host="intranet")) == ""
    assert base_url.known_hosts() == {}
    assert not path.exists()


def test_known_hosts_survives_a_damaged_file(tmp_path, monkeypatch):
    """A half-written or hand-edited file must not break every request."""
    path = tmp_path / "self_hosts.json"
    monkeypatch.setattr(base_url, "REMEMBERED_PATH", path)
    path.write_text("{not json", encoding="utf-8")
    assert base_url.known_hosts() == {}
    assert base_url.remember(request(_host="node.example")) == "node.example"
    assert list(base_url.known_hosts()) == ["node.example"]


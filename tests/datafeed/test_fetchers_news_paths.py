"""fetch_feed / fetch_web through a real httpx client on a mock transport."""
import logging
import subprocess
import sys

import httpx
import pytest

from agentic_fx.datafeed.fetchers import fetch_feed, fetch_web
from agentic_fx.datafeed.user_agent import user_agent
from tests.datafeed.news_support import ARTICLE_HTML, route_httpx_get, rss, rss_item

CANARY_Q = "ZQcanaryQ9xk"
CANARY_P = "ZPcanaryP7mw"
URL = f"https://example.invalid/{CANARY_P}/feed?k={CANARY_Q}"


def _serve(monkeypatch, body: bytes, *, seen=None, status=200, content_type="application/xml"):
    patch = monkeypatch.context()
    ctx = patch.__enter__()
    route_httpx_get(ctx, lambda request: httpx.Response(status, content=body,
                                                         headers={"content-type": content_type}),
                    seen)
    return patch


@pytest.fixture
def serve(monkeypatch):
    opened = []

    def start(body, **kw):
        opened.append(_serve(monkeypatch, body, **kw))

    yield start
    for patch in opened:
        patch.__exit__(None, None, None)


@pytest.mark.parametrize("fn,body,ctype", [
    (fetch_feed, rss(rss_item("https://example.invalid/a")), "application/xml"),
    (fetch_web, ARTICLE_HTML.encode(), "text/html; charset=utf-8"),
])
def test_both_fetchers_send_the_package_user_agent(serve, fn, body, ctype):
    seen = []
    serve(body, seen=seen, content_type=ctype)
    fn(URL, "src", timeout_sec=5)
    assert [r.headers["user-agent"] for r in seen] == [user_agent()]
    assert user_agent().startswith("agentic-fx/") and "python-httpx" not in user_agent()


def test_unrecognized_timezone_warning_names_the_source_not_the_url(serve, caplog):
    serve(rss(
        rss_item("https://example.invalid/jst", pub="Wed, 29 Jul 2026 18:20:42 JST"),
        rss_item("https://example.invalid/gmt", pub="Wed, 29 Jul 2026 18:20:42 GMT"),
        rss_item("https://example.invalid/off", pub="Wed, 29 Jul 2026 18:20:42 +0900")))
    with caplog.at_level(logging.DEBUG):
        arts = fetch_feed(URL, "my-source", timeout_sec=5)
    assert len(arts) == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "my-source" in text and "JST" in text
    assert CANARY_Q not in text and CANARY_P not in text and "example.invalid" not in text
    assert CANARY_Q not in repr(warnings[0].args)


def test_feed_links_other_than_http_and_https_are_skipped(serve, caplog):
    serve(rss(
        rss_item("https://example.invalid/ok-https"),
        rss_item("http://example.invalid/ok-http"),
        rss_item("web:my-web-source"),
        rss_item("javascript:alert(1)"),
        rss_item("ftp://example.invalid/x"),
        rss_item("/relative/path"),
        rss_item("http://[bad"),
        rss_item("HTTPS://example.invalid/upper-scheme")))
    with caplog.at_level(logging.DEBUG):
        arts = fetch_feed(URL, "src", timeout_sec=5)
    assert [a.url for a in arts] == ["https://example.invalid/ok-https", "http://example.invalid/ok-http",
                                     "HTTPS://example.invalid/upper-scheme"]
    assert all(a.rag_id is None for a in arts)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "web:my-web-source" not in text and "javascript" not in text


def test_feed_article_urls_keep_their_own_spelling(serve):
    link = "https://example.invalid/story?id=7&ref=a"
    serve(rss(rss_item(link)))
    assert [a.url for a in fetch_feed(URL, "src", timeout_sec=5)] == [link]


def test_web_article_hides_query_and_fragment_and_uses_the_source_name_as_id(serve):
    serve(ARTICLE_HTML.encode(), content_type="text/html; charset=utf-8")
    [art] = fetch_web(f"https://example.invalid/{CANARY_P}/page?k={CANARY_Q}", "web-src", timeout_sec=5)
    assert art.url == f"https://example.invalid/{CANARY_P}/page"
    assert art.rag_id == "web:web-src"
    assert CANARY_Q not in art.url and CANARY_Q not in art.title and CANARY_Q not in art.body
    assert art.source_name == "web-src"


def test_web_title_without_a_page_title_is_the_source_name_never_the_url(serve):
    serve(ARTICLE_HTML.encode(), content_type="text/html; charset=utf-8")
    [art] = fetch_web(f"https://example.invalid/{CANARY_P}/page?k={CANARY_Q}", "web-src", timeout_sec=5)
    assert art.title == "web-src"


def test_web_display_url_drops_userinfo_and_keeps_ipv6_and_port(serve):
    serve(ARTICLE_HTML.encode(), content_type="text/html; charset=utf-8")
    [art] = fetch_web("https://user:pw@[2001:db8::1]:8443/p?k=v", "web-src", timeout_sec=5)
    assert art.url == "https://[2001:db8::1]:8443/p"


def test_web_extraction_failure_is_an_empty_list(serve):
    serve(b"<html><body></body></html>", content_type="text/html")
    assert fetch_web(URL, "src", timeout_sec=5) == []


def test_httpx_request_lines_do_not_reach_a_debug_root_handler(serve):
    serve(rss(rss_item("https://example.invalid/a")))
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    root = logging.getLogger()
    handler = Capture(logging.DEBUG)
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        fetch_feed(URL, "src", timeout_sec=5)
        fetch_web(URL, "src", timeout_sec=5)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    assert not [m for m in records if CANARY_Q in m or CANARY_P in m]


def test_importing_the_fetch_module_pins_library_levels_and_adds_no_root_handler():
    code = (
        "import logging;"
        "before=list(logging.getLogger().handlers);"
        "import agentic_fx.datafeed.fetchers;"
        "assert logging.getLogger().handlers == before;"
        "assert logging.getLogger('httpx').getEffectiveLevel() >= logging.WARNING;"
        "assert logging.getLogger('httpcore').getEffectiveLevel() >= logging.WARNING;"
        "assert logging.getLogger('trafilatura').getEffectiveLevel() >= logging.WARNING")
    assert subprocess.run([sys.executable, "-I", "-c", code]).returncode == 0

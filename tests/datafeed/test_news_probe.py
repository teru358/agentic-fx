import io
from datetime import datetime, timezone

import httpx
import pytest

from agentic_fx.datafeed import news_probe
from agentic_fx.datafeed.fetchers import Article, FeedFetchError
from agentic_fx.datafeed.news_probe import (
    NewsInputError, ProbeFailure, classify_failure, fold_url, normalize_url, probe,
    read_url_from_stdin, validate_name, validate_url,
)
from tests.datafeed.news_support import raised_status

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
CANARY = "ZQcanaryQ9xk"
RSS = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
       b"<item><title>a</title><link>https://example.invalid/a</link>"
       b"<description>d</description></item></channel></rss>")


# ---- names ----------------------------------------------------------------

@pytest.mark.parametrize("name", ["a", "0", "a-b", "a" + "b" * 39, "9" * 40, "a--"])
def test_valid_names(name):
    validate_name(name)


@pytest.mark.parametrize("name", ["", "-a", "A", "aB", "a" + "b" * 40, "a_b", "a b", "a.b",
                                  "ａ", "a\n", None, 5])
def test_invalid_names(name):
    with pytest.raises(NewsInputError):
        validate_name(name)


# ---- urls -----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://example.invalid/feed", "https://example.invalid", "HTTPS://EXAMPLE.invalid/x?y=1",
    "https://[2001:db8::1]/feed?k=v", "https://192.0.2.1:8443/x", "https://example.invalid/feed?",
    "https://example.invalid/" + "a" * (2048 - len("https://example.invalid/")),
])
def test_valid_urls(url):
    validate_url(url)


@pytest.mark.parametrize("url", [
    "ftp://example.invalid/x", "file:///etc/passwd", "javascript:alert(1)", "//example.invalid/x",
    "example.invalid/x", "https:///x", "https://user@example.invalid/x",
    "https://user:pw@example.invalid/x", "https://example.invalid/x#frag",
    "https://example.invalid/x#", "https://example.invalid/#", "https://example.invalid:99999/x",
    "https://example.invalid:abc/x", "", None,
    "http://exa mple.com/", "http://example.com/\x00", "http://example.com/a\tb",
    "http://example.com/\n", "http://example.com/a\x7fb", "http://example.com/a b",
    "https://example.invalid/" + "a" * (2049 - len("https://example.invalid/")),
])
def test_invalid_urls(url):
    with pytest.raises(NewsInputError):
        validate_url(url)


def test_url_error_text_never_contains_the_input():
    for url in (f"https://u:{CANARY}@example.invalid/x", f"ftp://example.invalid/{CANARY}",
                f"https://example.invalid/{CANARY}#"):
        with pytest.raises(NewsInputError) as info:
            validate_url(url)
        assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    with pytest.raises(NewsInputError) as info:
        validate_name(CANARY.upper())
    assert CANARY.upper() not in str(info.value)


def test_length_is_counted_on_the_input_as_given():
    # percent-encoding or normalization would not change this; the raw length decides
    base = "https://EXAMPLE.invalid:443/"
    ok = base + "a" * (2048 - len(base))
    validate_url(ok)
    with pytest.raises(NewsInputError):
        validate_url(ok + "a")


@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://EXAMPLE.INVALID/Path?Q=A", "https://example.invalid/Path?Q=A"),
    ("http://example.invalid:80/x", "http://example.invalid/x"),
    ("https://example.invalid:443/x", "https://example.invalid/x"),
    ("https://example.invalid:80/x", "https://example.invalid:80/x"),
    ("http://example.invalid:443/x", "http://example.invalid:443/x"),
    ("https://example.invalid:8443/x", "https://example.invalid:8443/x"),
    ("https://example.invalid", "https://example.invalid"),
    ("https://example.invalid/", "https://example.invalid/"),
    ("https://example.invalid/feed?", "https://example.invalid/feed?"),
    ("https://example.invalid?", "https://example.invalid?"),
    ("https://[2001:DB8::1]/feed", "https://[2001:db8::1]/feed"),
    ("https://[2001:db8::1]:443/feed?k=v", "https://[2001:db8::1]/feed?k=v"),
    ("https://[2001:db8::1]:8443/feed", "https://[2001:db8::1]:8443/feed"),
    ("https://example.invalid/a%2Fb?x=%7e", "https://example.invalid/a%2Fb?x=%7e"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_empty_path_and_slash_stay_different_and_normalizing_twice_is_stable():
    assert normalize_url("https://example.invalid") != normalize_url("https://example.invalid/")
    for raw in ("HTTP://A.invalid:80/x?", "https://[2001:db8::1]:443"):
        once = normalize_url(raw)
        assert normalize_url(once) == once


def test_normalize_rejects_what_validate_rejects():
    with pytest.raises(NewsInputError):
        normalize_url("https://example.invalid/x#")


@pytest.mark.parametrize("raw,expected", [
    ("https://example.invalid/path/feed?k=v", "https://example.invalid/…"),
    ("HTTP://Example.INVALID:80/x", "http://example.invalid/…"),
    ("https://example.invalid:8443/x?k=v", "https://example.invalid:8443/…"),
    ("https://[2001:db8::1]/feed?k=v", "https://[2001:db8::1]/…"),
    ("https://[2001:db8::1]:8443/feed", "https://[2001:db8::1]:8443/…"),
    ("https://example.invalid", "https://example.invalid/…"),
])
def test_fold_url_hides_path_and_query(raw, expected):
    assert fold_url(raw) == expected


# ---- stdin ----------------------------------------------------------------

class Tty(io.StringIO):
    def isatty(self):
        return True


def _no_getpass(prompt=""):
    raise AssertionError("getpass must not be used for a pipe")


def test_pipe_reads_first_line_and_strips():
    assert read_url_from_stdin(io.StringIO("  https://example.invalid/x?k=1 \r\n"),
                               getpass_fn=_no_getpass) == "https://example.invalid/x?k=1"
    assert read_url_from_stdin(io.StringIO("https://example.invalid/x"),
                               getpass_fn=_no_getpass) == "https://example.invalid/x"


@pytest.mark.parametrize("text", ["", "\n", "   \n", "https://example.invalid/a\nhttps://example.invalid/b\n",
                                  "https://example.invalid/a\n\n"])
def test_pipe_rejects_empty_or_second_line(text):
    with pytest.raises(NewsInputError):
        read_url_from_stdin(io.StringIO(text), getpass_fn=_no_getpass)


def test_pipe_length_limit_is_2048_url_characters():
    url = "https://example.invalid/" + "a" * (2048 - len("https://example.invalid/"))
    assert read_url_from_stdin(io.StringIO(url + "\n"), getpass_fn=_no_getpass) == url
    assert read_url_from_stdin(io.StringIO(url), getpass_fn=_no_getpass) == url
    for text in (url + "a", url + "a\n", url + "aaaa\n"):
        with pytest.raises(NewsInputError):
            read_url_from_stdin(io.StringIO(text), getpass_fn=_no_getpass)


def test_pipe_never_reads_more_than_2049_characters_of_a_huge_line():
    class Counting(io.StringIO):
        read_chars = 0

        def readline(self, size=-1):
            line = super().readline(size)
            Counting.read_chars += len(line)
            return line

    stream = Counting("x" * 10_000_000 + "\n")
    with pytest.raises(NewsInputError):
        read_url_from_stdin(stream, getpass_fn=_no_getpass)
    assert Counting.read_chars <= 2049


def test_tty_uses_getpass_and_never_reads_the_stream():
    stream = Tty("must not be read\n")
    asked = []

    def getpass_fn(prompt=""):
        asked.append(prompt)
        return "  https://example.invalid/x?k=1\n"

    assert read_url_from_stdin(stream, getpass_fn=getpass_fn) == "https://example.invalid/x?k=1"
    assert len(asked) == 1
    assert stream.tell() == 0
    for bad in ("", "  ", "https://a.invalid/\nhttps://b.invalid/", "x" * 2049):
        with pytest.raises(NewsInputError):
            read_url_from_stdin(Tty(), getpass_fn=lambda prompt="", v=bad: v)


# ---- classify and probe ----------------------------------------------------

def test_classify_failure_table():
    ra = raised_status(429, {"Retry-After": "120"})
    got = classify_failure(ra, NOW)
    assert (got.reason, got.kind) == ("HTTP 429", "refusal")
    assert got.retry_after.total_seconds() == 120
    for exc, reason in [
        (raised_status(429), "HTTP 429"),
        (raised_status(503, {"Retry-After": "soon"}), "HTTP 503"),
        (raised_status(500, {"Retry-After": "120"}), "HTTP 500"),
        (raised_status(404), "HTTP 404"),
    ]:
        got = classify_failure(exc, NOW)
        assert (got.reason, got.kind, got.retry_after) == (reason, "failure", None)
    assert classify_failure(httpx.ReadTimeout("t"), NOW).reason == "タイムアウト"
    assert classify_failure(httpx.ConnectTimeout("t"), NOW).reason == "タイムアウト"
    assert classify_failure(httpx.ConnectError("c"), NOW).reason == "接続できません"
    assert classify_failure(httpx.ReadError("c"), NOW).reason == "接続できません"
    for other in (FeedFetchError("x"), RuntimeError("y"), ValueError("z"), httpx.InvalidURL("u")):
        got = classify_failure(other, NOW)
        assert (got.reason, got.kind, got.retry_after) == ("取得できません", "failure", None)


@pytest.mark.parametrize("exc,reason", [
    (raised_status(404, url=f"https://example.invalid/{CANARY}?k={CANARY}"), "HTTP 404"),
    (raised_status(429, {"Retry-After": "9"}), "HTTP 429"),
    (httpx.ReadTimeout(f"timeout for https://example.invalid/{CANARY}"), "タイムアウト"),
    (httpx.ConnectError(f"cannot reach https://[2001:db8::1]/{CANARY}"), "接続できません"),
    (RuntimeError(f"https://[2001:db8::1]/feed?k={CANARY}"), "取得できません"),
    (FeedFetchError(f"s: {CANARY}"), "取得できません"),
])
def test_probe_folds_every_failure_to_a_fixed_word(monkeypatch, exc, reason):
    def fetch(url, name, *, timeout_sec):
        raise exc
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.fetchers.fetch_feed", fetch)
        with pytest.raises(ProbeFailure) as info:
            probe("feed", "https://example.invalid/x", "src", timeout_sec=5)
    failure = info.value
    assert failure.reason == reason
    assert str(failure) == reason and failure.args == (reason,)
    assert failure.__cause__ is None and failure.__context__ is None
    assert CANARY not in repr(failure)


def test_probe_counts_articles_and_bodies_and_fetches_once(monkeypatch):
    calls = []

    def fetch(url, name, *, timeout_sec):
        calls.append((url, name, timeout_sec))
        return [Article("https://e.invalid/1", "t", "body", None, name),
                Article("https://e.invalid/2", "t", "", None, name)]

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.fetchers.fetch_web", fetch)
        result = probe("web", "https://example.invalid/x", "src", timeout_sec=7)
    assert (result.count, result.with_body) == (2, 1)
    assert calls == [("https://example.invalid/x", "src", 7)]


def test_probe_unknown_fetcher_makes_no_request(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.fetchers.fetch_feed", lambda *a, **k: 1 / 0)
        patch.setattr("agentic_fx.datafeed.fetchers.fetch_web", lambda *a, **k: 1 / 0)
        with pytest.raises(NewsInputError):
            probe("bogus", "https://example.invalid/x", "src", timeout_sec=5)


def test_probe_does_not_swallow_interrupts(monkeypatch):
    def fetch(url, name, *, timeout_sec):
        raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.fetchers.fetch_feed", fetch)
        with pytest.raises(KeyboardInterrupt):
            probe("feed", "https://example.invalid/x", "src", timeout_sec=5)


def test_probe_sends_the_user_agent_through_a_real_client(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=RSS)

    def mock_get(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.get(url, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "get", mock_get)
        result = probe("feed", "https://example.invalid/x", "src", timeout_sec=5)
    assert result.count == 1
    from agentic_fx.datafeed.user_agent import user_agent
    assert seen == [user_agent()] and not seen[0].startswith("python-httpx/")

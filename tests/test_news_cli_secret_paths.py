"""A source URL can carry a secret: no output path of ``afx news`` may show it."""
from __future__ import annotations

import logging
import warnings
from pathlib import Path

import httpx
import pytest

from agentic_fx import news_cli, status
from agentic_fx.datafeed import fetchers, news_probe
from agentic_fx.logging_setup import setup_technical_logging
from agentic_fx.store import news_health, news_sources
from tests.news_cli_support import (CANARY_P, CANARY_Q, IPV6_URL, NOW, RSS_EMPTY, RSS_TWO, SECRET_URL,
                                    FakeHttp, FakeStdin, db_path, dump_tables, make_root, run_news)
from tests.status_support import make_run

FIXED = "予期しない失敗のため中止しました。\n"
CANARIES = (CANARY_Q, CANARY_P)
FRAGMENT = f"/{CANARY_P}/feed?k={CANARY_Q}"


class Sweep:
    """Collects every observation point while commands run."""

    def __init__(self, root: Path, capsys, monkeypatch) -> None:
        self.root, self.capsys, self.monkeypatch = root, capsys, monkeypatch
        self.streams: list[str] = []
        self.warnings: list[str] = []
        self.root_records: list[str] = []
        self.results: list[tuple[int, str, str]] = []

    def run(self, *argv, stdin: FakeStdin | None = None):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = run_news(self.capsys, self.monkeypatch, self.root, *argv, stdin=stdin)
        self.warnings += [str(item.message) for item in caught]
        self.streams += [result[1], result[2]]
        self.results.append(result)
        return result

    def everything(self) -> str:
        status_out = self._status()
        parts = [*self.streams, *self.warnings, *self.root_records, status_out]
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path != db_path(self.root) and not path.name.startswith("agentic.db"):
                parts.append(path.read_bytes().decode("utf-8", "replace"))
        for table, rows in dump_tables(self.root, skip=frozenset({"news_sources"})).items():
            parts.append(repr((table, rows)))
        return "\n".join(parts)

    def _status(self) -> str:
        self.capsys.readouterr()
        with self.monkeypatch.context() as patch:
            patch.setattr(status, "default_unit_dir", lambda: self.root / "unit")
            status.main(self.root, run=make_run())
        return self.capsys.readouterr().out


class _Collect(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record.getMessage())


@pytest.fixture
def sweep_for(tmp_path, capsys, monkeypatch):
    app_logger, root_logger = logging.getLogger("agentic_fx"), logging.getLogger()
    saved = (app_logger.level, app_logger.propagate, list(app_logger.handlers), root_logger.level)
    created: list[Sweep] = []
    handler = None

    def make(*, sources=()):
        nonlocal handler
        root = make_root(tmp_path, sources=sources)
        setup_technical_logging(root / "logs", "DEBUG")
        sweep = Sweep(root, capsys, monkeypatch)
        handler = _Collect(sweep.root_records)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        created.append(sweep)
        return sweep

    yield make
    if handler is not None:
        root_logger.removeHandler(handler)
    for added in [h for h in app_logger.handlers if h not in saved[2]]:
        app_logger.removeHandler(added)
        added.close()
    app_logger.setLevel(saved[0])
    app_logger.propagate = saved[1]
    root_logger.setLevel(saved[3])


def _assert_clean(sweep: Sweep) -> None:
    text = sweep.everything()
    for canary in CANARIES:
        assert canary not in text, canary


URLS = [pytest.param(SECRET_URL, id="plain"), pytest.param(IPV6_URL, id="ipv6")]


@pytest.mark.parametrize("url", URLS)
def test_add_list_test_enable_disable_never_show_the_secret(sweep_for, url):
    sweep = sweep_for()
    http = FakeHttp(sweep.monkeypatch)
    assert sweep.run("add", "paid", url)[0] == 0
    assert sweep.run("add", "paid", url)[0] == 1                      # same name
    assert sweep.run("add", "again", url)[0] == 1                     # same URL
    assert sweep.run("list")[0] == 0
    assert sweep.run("test", "paid")[0] == 0
    assert sweep.run("disable", "paid")[0] == 0
    assert sweep.run("enable", "paid")[0] == 0
    out = sweep.results[3][1]
    assert "/…" in out and "k=" not in out
    assert http.count == 2
    _assert_clean(sweep)


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("failure", [
    dict(status=404),
    dict(status=429, headers={"Retry-After": "120"}),
    dict(raises=httpx.ConnectError("connect failed for SECRET")),
    dict(raises=httpx.ReadTimeout("timeout for SECRET")),
    dict(raises=RuntimeError("weird SECRET")),
])
def test_fetch_failures_are_fixed_words_without_the_url(sweep_for, url, failure):
    sweep = sweep_for(sources=(("paid", "feed", url, True),))
    exc = failure.get("raises")
    if exc is not None:
        failure = dict(failure, raises=type(exc)(str(exc).replace("SECRET", url)))
    FakeHttp(sweep.monkeypatch, **failure)
    rc, out, err = sweep.run("add", "other", url + "x", "--yes")
    assert (rc, out) == (1, "")
    assert err.startswith("取得に失敗したので追加しません: ")
    rc, out, err = sweep.run("test", "paid")
    assert (rc, out) == (1, "")
    assert err.startswith("取得に失敗しました: ")
    assert err.count("\n") == 1
    _assert_clean(sweep)


@pytest.mark.parametrize("url", URLS)
def test_zero_article_decisions_and_dash_inputs_do_not_echo_the_url(sweep_for, url):
    sweep = sweep_for()
    FakeHttp(sweep.monkeypatch, body=RSS_EMPTY)
    sweep.run("add", "a", url, stdin=FakeStdin("n\n", tty=True))
    sweep.run("add", "b", url, stdin=FakeStdin(""))
    sweep.run("add", "c", "-", stdin=FakeStdin(url + "\n"))
    sweep.monkeypatch.setattr(news_cli.getpass, "getpass", lambda prompt="": url)
    sweep.run("add", "d", "-", "--yes", stdin=FakeStdin("", tty=True))
    sweep.run("add", "e", "-", stdin=FakeStdin(""))
    sweep.run("add", "f", "-", stdin=FakeStdin(url + "\n" + url + "\n"))
    sweep.run("add", "g", "-", stdin=FakeStdin("x" * 2048 + url))
    sweep.monkeypatch.setattr(news_cli.getpass, "getpass", lambda prompt="": "x" * 2049 + url)
    sweep.run("add", "h", "-", stdin=FakeStdin("", tty=True))
    assert [result[0] for result in sweep.results] == [1, 1, 1, 0, 2, 2, 2, 2]
    _assert_clean(sweep)


@pytest.mark.parametrize("bad", [
    f"https://user:pw@example.invalid{FRAGMENT}",
    f"https://user@example.invalid{FRAGMENT}",
    f"https://example.invalid{FRAGMENT}#frag",
    f"https://example.invalid{FRAGMENT}#",
    f"https://example.invalid{FRAGMENT} tail",
    f"https://example.invalid{FRAGMENT}\ttail",
    f"https://example.invalid{FRAGMENT}\x00tail",
    f"ftp://example.invalid{FRAGMENT}",
    f"example.invalid{FRAGMENT}",
    f"https://example.invalid{FRAGMENT}" + "x" * 2049,
])
def test_grammar_violations_are_two_with_a_fixed_sentence(sweep_for, bad):
    sweep = sweep_for()
    http = FakeHttp(sweep.monkeypatch)
    rc, out, err = sweep.run("add", "paid", bad)
    assert (rc, out, err) == (2, "", "URL の形式が正しくありません\n")
    assert http.count == 0
    _assert_clean(sweep)


UNEXPECTED_TEXTS = [
    f"boom {SECRET_URL}",
    f"boom {IPV6_URL}",
    f"boom example.invalid{FRAGMENT}",
    f"boom ?k={CANARY_Q}",
    f"boom [2001:db8::1]/feed?k={CANARY_Q}",
]


def _raiser(text: str, *, chained: bool):
    def raise_it(*args, **kwargs):
        if chained:
            try:
                raise ValueError(text)
            except ValueError as cause:
                raise RuntimeError() from cause
        raise RuntimeError(text)
    return raise_it


@pytest.mark.parametrize("chained", [False, True], ids=["message", "cause"])
@pytest.mark.parametrize("text", UNEXPECTED_TEXTS)
@pytest.mark.parametrize("target", ["list", "add", "enable", "disable", "test"])
def test_unexpected_exceptions_print_one_fixed_sentence_and_nothing_else(sweep_for, target, text, chained):
    sweep = sweep_for(sources=(("paid", "feed", SECRET_URL, target == "disable"),))
    FakeHttp(sweep.monkeypatch, body=RSS_EMPTY)
    raiser = _raiser(text, chained=chained)
    argv = {"list": ("list",), "add": ("add", "fresh", "https://example.invalid/new", "--yes"),
            "enable": ("enable", "paid"), "disable": ("disable", "paid"), "test": ("test", "paid")}[target]
    patched = {"list": (news_sources, "list_all"), "add": (news_sources, "find_duplicate"),
               "enable": (news_health, "initialize_for_enable"), "disable": (news_sources, "set_enabled"),
               "test": (news_health, "read_effective_wait")}[target]
    sweep.monkeypatch.setattr(*patched, raiser)
    rc, out, err = sweep.run(*argv)
    assert (rc, err) == (1, FIXED)
    assert "Traceback" not in out + err
    _assert_clean(sweep)


def test_an_unexpected_exception_after_the_normal_output_keeps_that_output_but_a_fixed_stderr(sweep_for):
    sweep = sweep_for()
    FakeHttp(sweep.monkeypatch, body=RSS_TWO)
    sweep.monkeypatch.setattr(news_sources, "add", _raiser(f"boom {SECRET_URL}", chained=True))
    rc, out, err = sweep.run("add", "fresh", "https://example.invalid/new")
    assert rc == 1 and out == "取得: 2 件 (うち本文・要約あり 1 件)\n"
    assert err == FIXED
    _assert_clean(sweep)


def test_a_probe_failure_carries_no_chained_exception(sweep_for):
    sweep = sweep_for()
    FakeHttp(sweep.monkeypatch, raises=httpx.ConnectError(f"for url {SECRET_URL}"))
    with pytest.raises(news_probe.ProbeFailure) as raised:
        news_probe.probe("feed", SECRET_URL, "x", timeout_sec=1.0)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert CANARY_Q not in str(raised.value)


def test_the_httpx_request_log_never_reaches_a_debug_root_handler(sweep_for):
    sweep = sweep_for()
    FakeHttp(sweep.monkeypatch)
    assert sweep.run("add", "paid", SECRET_URL)[0] == 0
    assert sweep.root_records is not None
    assert not any("example.invalid" in record for record in sweep.root_records)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_list_prints_only_scheme_and_host_so_a_path_or_query_mutation_would_fail(sweep_for):
    sweep = sweep_for(sources=(("paid", "feed", SECRET_URL, True), ("v6", "feed", IPV6_URL, True)))
    rc, out, _ = sweep.run("list")
    assert rc == 0
    assert "  https://example.invalid/…" in out.splitlines()
    assert "  https://[2001:db8::1]/…" in out.splitlines()
    assert CANARY_P not in out and CANARY_Q not in out


def test_the_stored_url_is_the_only_place_the_secret_lives(sweep_for):
    sweep = sweep_for()
    FakeHttp(sweep.monkeypatch)
    assert sweep.run("add", "paid", SECRET_URL)[0] == 0
    stored = dump_tables(sweep.root)["news_sources"][0][3]
    assert stored == SECRET_URL
    assert not (sweep.root / "logs" / "activity.log").read_text().count(CANARY_Q)

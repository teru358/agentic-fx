"""The news-source sub-step of the price stage of `afx init`."""
from __future__ import annotations

import logging
import sqlite3
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_fx import status
from agentic_fx.datafeed import news_probe
from agentic_fx.datafeed.news_probe import ProbeFailure, ProbeResult
from agentic_fx.setup import wizard
from agentic_fx.setup.preflight import PreflightFailure
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store import news_sources
from agentic_fx.store.setup_marker import setup_marker_present
from tests.setup.test_wizard_commit import db_rows
from tests.setup.wizard_support import (
    FakeResponse, Rules, ScriptedWizardIO, fresh_responder, make_deps, make_root,
)
from tests.status_support import make_run

FEED = "https://example.invalid/feed"
ENTRY = f"nhk-keizai {FEED}"
ASK = "ニュース源"
CLOSING = "後から `afx news add` で足せます。候補例は README を参照。"


class ProbeSpy:
    """Replacement for `news_probe.probe`: records calls, answers per name."""

    def __init__(self, outcomes: dict | None = None, default=ProbeResult(3, 2)) -> None:
        self.calls: list[tuple[str, str, str, float]] = []
        self.outcomes = outcomes or {}
        self.default = default

    def __call__(self, fetcher, url, name, *, timeout_sec):
        self.calls.append((fetcher, url, name, timeout_sec))
        outcome = self.outcomes.get(name, self.default)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def go(tmp_path, *extra, spy=None, root=None, deps_kwargs=None, hook=None, deps=None):
    """Run the whole wizard with a probe stub; returns (rc, io, root, spy)."""
    root = root or make_root(tmp_path)
    spy = spy or ProbeSpy()
    if deps is None:
        deps_kwargs = dict(deps_kwargs or {})
        if hook is not None:
            deps_kwargs["hook"] = hook
        deps, *_ = make_deps(tmp_path, **deps_kwargs)
    io = ScriptedWizardIO(responder=fresh_responder(*extra))
    with pytest.MonkeyPatch.context() as m:
        m.setattr(news_probe, "probe", spy)
        rc = run_wizard(root, io, deps)
    return rc, io, root, spy


def sources_of(root: Path) -> list[dict]:
    conn = sqlite3.connect(root / "data" / "agentic.db")
    conn.row_factory = sqlite3.Row
    try:
        return news_sources.list_all(conn)
    finally:
        conn.close()


def entries(*lines: str):
    return (ASK, tuple(lines) + ("",))


# --- the stage function itself -------------------------------------------------

def test_a_valid_entry_is_kept_for_the_commit_and_probed_once_with_the_configured_timeout(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    session = wizard._Session(root, ScriptedWizardIO([ENTRY, ""]), deps)
    start = wizard._stage0(session)
    answers = wizard.Answers("UTC", "JPY", ["USDJPY"], 1_000_000)
    spy = ProbeSpy(default=ProbeResult(2, 1))
    with pytest.MonkeyPatch.context() as m:
        m.setattr(news_probe, "probe", spy)
        wizard._stage3_news_sources(session, start, answers)
    assert answers.news_sources == [("nhk-keizai", FEED)]
    assert spy.calls == [("feed", FEED, "nhk-keizai", start.base_settings.worker.data_hook_timeout_sec)]
    assert "取得: 2 件 (うち本文・要約あり 1 件)" in session.io.text
    assert CLOSING in session.io.text


# --- every exit of the price stage reaches the sub-step exactly once -----------

KEY = "bridge-key-placeholder"


def _plain_http_fetch(default_fetch):
    def fetch(url, **kwargs):
        if url.endswith("/health"):
            return FakeResponse({"mt5_connected": True})
        if "/quote/" in url:
            return FakeResponse({}, 401)
        return default_fetch(url, **kwargs)
    return fetch


def _broken_yf(pair):
    raise RuntimeError("upstream failed")


EXITS = {
    "bridge-connected": dict(extra=()),
    "bridge-plain-http-key-withheld": dict(
        extra=(("bridge の URL", "http://bridge.example:8812"), ("bridge 側で API キー", "1")),
        environ={"MT5_BRIDGE_API_KEY": KEY}, plain_http=True),
    "bridge-failed-then-planned": dict(extra=(("[r] 再試行", "2"),), bridge_status=500),
    "planned-without-test": dict(extra=(("価格源", "2"),)),
    "planned-with-test-connected": dict(extra=(("価格源", "2"), ("今 bridge への接続を試しますか", "y"))),
    "planned-with-test-failed": dict(extra=(("価格源", "2"), ("今 bridge への接続を試しますか", "y")),
                                     bridge_status=500),
    "yfinance-probe-ok": dict(extra=(("価格源", ("3", "1")),)),
    "yfinance-probe-declined": dict(extra=(("価格源", ("3", "1")), ("yfinance に", "n"))),
    "yfinance-probe-failed": dict(extra=(("価格源", ("3", "1")),), yf_quote=_broken_yf),
    "twelvedata": dict(extra=(("価格源", ("3", "2")),), environ={"TWELVEDATA_API_KEY": "x"}),
}


@pytest.mark.parametrize("name", sorted(EXITS))
def test_every_exit_of_the_price_stage_asks_for_news_sources_exactly_once(tmp_path, name):
    spec = EXITS[name]
    deps_kwargs = {key: spec[key] for key in ("environ", "yf_quote") if key in spec}
    deps, calls, fetch, run = make_deps(tmp_path, **deps_kwargs)
    if "bridge_status" in spec:
        fetch.bridge_status = spec["bridge_status"]
    if spec.get("plain_http"):
        deps = replace(deps, fetch=_plain_http_fetch(fetch))
    rc, io, root, spy = go(tmp_path, *spec["extra"], entries(ENTRY), deps=deps)
    assert rc == 0, io.text
    asked = io.asked_with(ASK)
    assert len(asked) == 2  # the entry, then the empty line that ends the sub-step
    assert io.text.count(CLOSING) == 1
    assert io.asked.index(asked[0]) > io.asked.index(io.asked_with("価格源")[0])
    assert len(spy.calls) == 1
    assert [row["name"] for row in sources_of(root)] == ["nhk-keizai"]


# --- what is registered ---------------------------------------------------------

def test_exactly_the_entered_sources_are_registered_enabled_with_the_normalized_url(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(
        "aa-one HTTPS://Example.INVALID:443/a?x=1", "bb-two http://example.invalid:80/b"))
    assert rc == 0, io.text
    rows = sources_of(root)
    assert [(r["name"], r["fetcher"], r["url"], r["enabled"], r["added_by"]) for r in rows] == [
        ("aa-one", "feed", "https://example.invalid/a?x=1", 1, "user"),
        ("bb-two", "feed", "http://example.invalid/b", 1, "user")]
    assert [call[:3] for call in spy.calls] == [
        ("feed", "https://example.invalid/a?x=1", "aa-one"), ("feed", "http://example.invalid/b", "bb-two")]


def test_an_empty_enter_registers_nothing_and_says_where_to_add_later(tmp_path):
    rc, io, root, spy = go(tmp_path)
    assert rc == 0, io.text
    assert sources_of(root) == [] and spy.calls == []
    assert io.text.count(CLOSING) == 1


@pytest.mark.parametrize("line,reason", [
    ("Bad_Name https://example.invalid/a", "名前の形式が正しくありません"),
    ("ok-name ftp://example.invalid/a", "URL の形式が正しくありません"),
    ("ok-name https://user:pw@example.invalid/a", "URL の形式が正しくありません"),
    ("ok-name https://example.invalid/a#frag", "URL の形式が正しくありません"),
    ("ok-name https://example.invalid/a\x01b", "URL の形式が正しくありません"),
    ("ok-name https://example.invalid/" + "a" * 2040, "URL の形式が正しくありません"),
    ("only-a-name", "名前と URL を空白で区切って入力してください。"),
    ("a b c", "名前と URL を空白で区切って入力してください。")])
def test_a_malformed_entry_is_refused_with_a_reason_and_asked_again_without_fetching(tmp_path, line, reason):
    rc, io, root, spy = go(tmp_path, entries(line))
    assert rc == 0, io.text
    assert reason in io.text
    assert spy.calls == [] and sources_of(root) == []
    assert len(io.asked_with(ASK)) == 2


def test_a_failed_retrieval_is_reported_by_its_fixed_word_and_nothing_is_registered(tmp_path):
    spy = ProbeSpy({"aa-bad": ProbeFailure("HTTP 404")})
    rc, io, root, spy = go(tmp_path, entries(f"aa-bad {FEED}", f"bb-good {FEED}x"), spy=spy)
    assert rc == 0, io.text
    assert "取得に失敗したので追加しません: HTTP 404" in io.text
    assert [row["name"] for row in sources_of(root)] == ["bb-good"]
    assert len(io.asked_with(ASK)) == 3


@pytest.mark.parametrize("answer,registered", [("y", True), ("n", False), ("", False)])
def test_zero_articles_need_a_yes_before_the_source_is_added(tmp_path, answer, registered):
    spy = ProbeSpy({"empty-one": ProbeResult(0, 0)})
    rc, io, root, spy = go(tmp_path, ("0 件でした", answer), entries(f"empty-one {FEED}"), spy=spy)
    assert rc == 0, io.text
    assert "取得: 0 件 (うち本文・要約あり 0 件)" in io.text
    assert [row["name"] for row in sources_of(root)] == (["empty-one"] if registered else [])


def test_a_nonzero_count_is_never_confirmed(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(ENTRY))
    assert rc == 0, io.text
    assert "それでも追加しますか" not in "".join(io.asked)


def test_duplicates_of_this_input_are_refused_before_any_fetch(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(
        ENTRY, f"nhk-keizai {FEED}2", f"other-name {FEED}", f"other-name HTTPS://EXAMPLE.invalid:443/feed"))
    assert rc == 0, io.text
    assert io.text.count("既に登録されています: nhk-keizai") == 3
    assert len(spy.calls) == 1
    assert [row["name"] for row in sources_of(root)] == ["nhk-keizai"]


def test_duplicates_of_the_database_are_refused_before_any_fetch(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(ENTRY))
    assert rc == 0, io.text
    rc, io, root, spy = go(tmp_path / "again", entries(
        f"nhk-keizai {FEED}2", f"other-name {FEED}", f"fresh-one {FEED}3"), root=root)
    assert rc == 0, io.text
    assert io.text.count("既に登録されています: nhk-keizai") == 2
    assert [call[2] for call in spy.calls] == ["fresh-one"]
    assert [row["name"] for row in sources_of(root)] == ["nhk-keizai", "fresh-one"]


# --- going back to the stage keeps what was entered -----------------------------

def test_going_back_shows_the_kept_names_and_neither_refetches_nor_registers_twice(tmp_path, monkeypatch):
    real = wizard.service._check_mission_ceilings
    calls = {"n": 0}

    def fail_once(settings, activity):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PreflightFailure("other_problem", "値は質問への答えでは直せません")
        return real(settings, activity)

    monkeypatch.setattr(wizard.service, "_check_mission_ceilings", fail_once)
    rc, io, root, spy = go(tmp_path, ("[a] 中止して", "r"), entries(ENTRY, f"bb-two {FEED}2"))
    assert rc == 0, io.text
    assert calls["n"] == 2
    assert len(spy.calls) == 2
    assert "入力済みのニュース源: nhk-keizai, bb-two" in io.text
    assert "example.invalid" not in io.text
    assert [row["name"] for row in sources_of(root)] == ["nhk-keizai", "bb-two"]


# --- the commit ------------------------------------------------------------------

def test_a_source_added_by_someone_else_after_the_fetch_is_skipped_and_the_commit_goes_on(tmp_path):
    def rival(name):
        if name == "after_init_db":
            conn = sqlite3.connect(tmp_path / "root" / "data" / "agentic.db")
            try:
                conn.execute(
                    "INSERT INTO news_sources (name, fetcher, url, enabled, added_by, created_at) "
                    "VALUES ('nhk-keizai','feed','https://rival.invalid/x',0,'user','2026-10-02T00:00:00+00:00')")
                conn.commit()
            finally:
                conn.close()

    rc, io, root, spy = go(tmp_path, entries(ENTRY, f"bb-two {FEED}2"), hook=rival)
    assert rc == 0, io.text
    rows = sources_of(root)
    assert [(r["name"], r["url"], r["enabled"]) for r in rows] == [
        ("nhk-keizai", "https://rival.invalid/x", 0), ("bb-two", f"{FEED}2", 1)]
    assert "既に登録されていたので飛ばしました: nhk-keizai" in io.text
    assert "既に登録されていたので飛ばしました: bb-two" not in io.text
    assert not setup_marker_present(root)


def test_a_rival_with_the_same_url_under_another_name_also_skips_the_entry(tmp_path):
    def rival(name):
        if name == "after_init_db":
            conn = sqlite3.connect(tmp_path / "root" / "data" / "agentic.db")
            try:
                conn.execute(
                    "INSERT INTO news_sources (name, fetcher, url, enabled, added_by, created_at) "
                    f"VALUES ('rival-name','feed','{FEED}',1,'user','2026-10-02T00:00:00+00:00')")
                conn.commit()
            finally:
                conn.close()

    rc, io, root, spy = go(tmp_path, entries(ENTRY), hook=rival)
    assert rc == 0, io.text
    assert [r["name"] for r in sources_of(root)] == ["rival-name"]
    assert "既に登録されていたので飛ばしました: nhk-keizai" in io.text


def test_a_failing_second_insert_leaves_no_source_no_consumed_period_and_the_marker(tmp_path, monkeypatch):
    real_add = news_sources.add
    seen = {"n": 0}

    def flaky(conn, **kwargs):
        seen["n"] += 1
        if seen["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real_add(conn, **kwargs)

    root = make_root(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(news_sources, "add", flaky)
        rc, io, root, spy = go(tmp_path, entries(ENTRY, f"bb-two {FEED}2"), root=root)
    assert rc == 1
    assert seen["n"] == 2
    assert setup_marker_present(root)
    assert db_rows(root) == {"news_sources": [], "improve_waves": []}
    assert "確定の途中で失敗しました" in io.text
    # the same answers converge on the rerun
    rc, io, root, spy = go(tmp_path / "again", entries(ENTRY, f"bb-two {FEED}2"), root=root)
    assert rc == 0, io.text
    assert not setup_marker_present(root)
    assert [r["name"] for r in sources_of(root)] == ["nhk-keizai", "bb-two"]
    assert len(db_rows(root)["improve_waves"]) == 1


@pytest.mark.parametrize("point,rows", [("before_seed_commit", 0), ("after_seed_commit", 1),
                                        ("after_setup_record_write", 1)])
def test_a_crash_around_the_database_commit_keeps_or_drops_the_sources_together_with_the_period(
        tmp_path, point, rows):
    class Crash(Exception):
        pass

    def hook(name):
        if name == point:
            raise Crash()

    rc, io, root, spy = go(tmp_path, entries(ENTRY), hook=hook)
    assert rc != 0
    assert setup_marker_present(root)
    state = db_rows(root)
    assert len(state["news_sources"]) == rows and len(state["improve_waves"]) == rows
    rc, io, root, spy = go(tmp_path / "again", entries(ENTRY), root=root)
    assert rc == 0, io.text
    assert [r["name"] for r in sources_of(root)] == ["nhk-keizai"]


# --- existing databases are never touched ----------------------------------------

def test_a_rerun_leaves_registered_sources_untouched_and_does_not_re_enable_a_disabled_one(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(ENTRY, f"bb-two {FEED}2", f"cc-three {FEED}3"))
    assert rc == 0, io.text
    conn = sqlite3.connect(root / "data" / "agentic.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = news_sources.list_all(conn)
        news_sources.set_enabled(conn, rows[1]["id"], False)
        before = news_sources.list_all(conn)
    finally:
        conn.close()
    assert [row["enabled"] for row in before] == [1, 0, 1]
    rc, io, root, spy = go(tmp_path / "again", root=root)
    assert rc == 0, io.text
    assert sources_of(root) == before
    assert spy.calls == []


def test_the_source_list_is_unreadable_but_the_wizard_still_runs_and_says_so(tmp_path):
    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "agentic.db").write_bytes(b"not a database")
    rc, io, root, spy = go(tmp_path, entries(ENTRY), root=root)
    assert "登録済みのニュース源を確認できませんでした。" in io.text


# --- secrets in URLs -------------------------------------------------------------

CANARY_Q = "CANARYQUERY7f3a91"
CANARY_P = "CANARYPATH5c20de"
HOST = "canary-host.example.invalid"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _everything_but_news_sources(root: Path) -> str:
    chunks: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".db" not in path.name:
            chunks.append(path.read_bytes().decode("utf-8", "replace"))
    conn = sqlite3.connect(root / "data" / "agentic.db")
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if table == "news_sources" or table.startswith("sqlite_") or table.startswith("news_sources"):
                continue
            try:
                chunks.append(repr(conn.execute(f"SELECT * FROM \"{table}\"").fetchall()))
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return "\n".join(chunks)


def test_no_url_secret_leaves_the_wizard_by_any_route(tmp_path, monkeypatch, capsys):
    real_normalize = news_probe.normalize_url

    def normalize(url):
        if "boom-validate" in url:
            raise RuntimeError(f"unexpected failure for https://[2001:db8::1]/feed?k={CANARY_Q} {url}")
        return real_normalize(url)

    long_url = f"https://{HOST}/{CANARY_P}?k={CANARY_Q}" + "a" * 2040
    lines = (
        f"ok-one https://{HOST}/{CANARY_P}/feed?k={CANARY_Q}",
        f"ok-v6 https://[2001:db8::1]/feed?k={CANARY_Q}",
        f"fail-one https://{HOST}/{CANARY_P}/f?k={CANARY_Q}",
        f"raise-one https://{HOST}/{CANARY_P}/r?k={CANARY_Q}",
        f"ok-one https://{HOST}/{CANARY_P}/other?k={CANARY_Q}",
        f"dup-url https://{HOST}/{CANARY_P}/feed?k={CANARY_Q}",
        f"empty-one https://{HOST}/{CANARY_P}/z?k={CANARY_Q}",
        f"user-one https://user:{CANARY_Q}@{HOST}/{CANARY_P}",
        f"frag-one https://{HOST}/{CANARY_P}#{CANARY_Q}",
        f"ctrl-one https://{HOST}/{CANARY_P}\x01?k={CANARY_Q}",
        f"long-one {long_url}",
        f"boom-validate https://{HOST}/{CANARY_P}/boom-validate?k={CANARY_Q}",
        f"Bad_Name https://{HOST}/{CANARY_P}?k={CANARY_Q}",
    )
    spy = ProbeSpy({
        "fail-one": ProbeFailure("接続できません"),
        "raise-one": RuntimeError(f"boom https://{HOST}/{CANARY_P}/r?k={CANARY_Q}"),
        "empty-one": ProbeResult(0, 0)})
    capture = _Capture()
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.addHandler(capture)
    root_logger.setLevel(logging.DEBUG)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with monkeypatch.context() as m:
                m.setattr(news_probe, "normalize_url", normalize)
                rc, io, root, spy = go(tmp_path, ("0 件でした", "n"), entries(*lines), spy=spy)
            assert rc == 0, io.text
            monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")
            capsys.readouterr()
            status.main(root, run=make_run())
            status_out = capsys.readouterr()
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(old_level)
    surfaces = {
        "screen": io.text + "\n".join(io.asked),
        "log": "\n".join(capture.lines),
        "warnings": "\n".join(str(w.message) for w in caught),
        "status": status_out.out + status_out.err,
        "files-and-tables": _everything_but_news_sources(root)}
    for label, text in surfaces.items():
        for canary in (CANARY_Q, CANARY_P, HOST, "2001:db8"):
            assert canary not in text, (label, canary)
    # the controls: both really were registered, so the greps above are not vacuous
    stored = {row["name"]: row["url"] for row in sources_of(root)}
    assert set(stored) == {"ok-one", "ok-v6"}
    assert CANARY_Q in stored["ok-one"] and CANARY_Q in stored["ok-v6"]
    assert "boom" not in surfaces["screen"] and "unexpected failure" not in surfaces["screen"]
    assert "取得に失敗したので追加しません: 接続できません" in io.text
    assert "取得に失敗したので追加しません: 取得できません" in io.text
    assert "ニュース源の入力を確認できませんでした。" in io.text


def test_the_summary_and_the_setup_record_carry_no_source_url(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(f"aa-one https://{HOST}/{CANARY_P}?k={CANARY_Q}"))
    assert rc == 0, io.text
    record = (root / "data" / "state" / "setup.json").read_text()
    assert CANARY_Q not in record and CANARY_P not in record and "aa-one" not in record
    assert CANARY_Q not in io.text


def test_the_timeout_follows_the_settings_value(tmp_path):
    rc, io, root, spy = go(tmp_path, entries(ENTRY))
    assert rc == 0, io.text
    from agentic_fx.config import load_settings_text
    start_default = load_settings_text(
        (root / "config" / "settings.yaml").read_text()).worker.data_hook_timeout_sec
    assert spy.calls[0][3] == start_default

"""``afx news``: entry, read-only guarantees, rc table and the five operations."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from agentic_fx import entry, news_cli
from agentic_fx.datafeed import news_probe
from agentic_fx.store import db as store_db
from agentic_fx.store import news_health, news_sources
from agentic_fx.store.db import connect, connect_existing_rw
from tests.fixtures.fs_tree import tree_snapshot
from tests.news_cli_support import (NOW, OK_URL, RSS_EMPTY, RSS_TWO, FakeHttp, FakeStdin, db_path,
                                    dump_tables, make_root, run_news)
from tests.status_support import add_sidecars

MISSING = "ニュース源を読めません (DB がありません)。\n"
FIVE = (("list",), ("test", "src"), ("add", "newsrc", OK_URL, "--yes"), ("enable", "src"), ("disable", "src"))
SOURCES = (("src", "feed", OK_URL, False),)


def _fresh(tmp_path, label):
    path = tmp_path / (str(abs(hash(label))))
    path.mkdir()
    return path


def _activity(root: Path) -> list[str]:
    path = root / "logs" / "activity.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# --- connect_existing_rw ---------------------------------------------------------------------

def test_existing_rw_connection_never_creates_a_database(tmp_path):
    path = tmp_path / "data" / "agentic.db"
    with pytest.raises(FileNotFoundError):
        connect_existing_rw(path)
    assert not path.parent.exists()


def test_existing_rw_connection_maps_a_vanishing_file_to_file_not_found(tmp_path, monkeypatch):
    path = tmp_path / "agentic.db"
    sqlite3.connect(path).close()
    real = Path.is_file

    def vanishing(self):
        found = real(self)
        if self == path:
            path.unlink()
        return found

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_file", vanishing)
        with pytest.raises(FileNotFoundError):
            connect_existing_rw(path)
    assert not path.exists()


def test_existing_rw_connection_configures_a_real_existing_database(tmp_path):
    path = tmp_path / "agentic.db"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=DELETE")
    seed.close()
    conn = connect_existing_rw(path)
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_existing_rw_connection_does_not_initialise_the_schema(tmp_path):
    path = tmp_path / "agentic.db"
    sqlite3.connect(path).close()
    conn = connect_existing_rw(path)
    try:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


# --- entry and missing database ----------------------------------------------------------------

def test_news_is_a_local_command_and_does_not_start_the_service(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(entry.service, "run_service", lambda *a, **k: pytest.fail("service started"))
    monkeypatch.setattr(entry.service, "run_init", lambda *a, **k: pytest.fail("init ran"))
    rc, out, err = run_news(capsys, monkeypatch, make_root(tmp_path, settings="missing"), "list")
    assert (rc, out, err) == (0, "ニュース源はありません。\n", "")


@pytest.mark.parametrize("argv", FIVE)
def test_missing_database_is_never_created_and_every_operation_returns_one(tmp_path, monkeypatch, capsys, argv):
    root = tmp_path / "bare"
    root.mkdir()
    monkeypatch.setattr(store_db, "init_db", lambda *a, **k: pytest.fail("init_db called"))
    monkeypatch.setattr(entry.service, "run_service", lambda *a, **k: pytest.fail("service started"))
    before = tree_snapshot(root)
    rc, out, err = run_news(capsys, monkeypatch, root, *argv)
    assert (rc, out, err) == (1, "", MISSING)
    assert tree_snapshot(root) == before


@pytest.mark.parametrize("argv", FIVE)
def test_uninitialised_root_is_not_gated_by_ensure_initialized(tmp_path, monkeypatch, capsys, argv):
    from agentic_fx import service
    root = tmp_path / "bare"
    (root / "config").mkdir(parents=True)
    monkeypatch.setattr(service, "ensure_initialized", lambda *a, **k: pytest.fail("gate consulted"))
    rc, _, err = run_news(capsys, monkeypatch, root, *argv)
    assert (rc, err) == (1, MISSING)


@pytest.mark.parametrize("argv", FIVE)
def test_database_removed_between_check_and_open_is_not_recreated(tmp_path, monkeypatch, capsys, argv):
    root = make_root(tmp_path, sources=SOURCES)
    path = db_path(root)
    real = Path.is_file

    def vanishing(self):
        found = real(self)
        if self == path:
            path.unlink()
        return found

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_file", vanishing)
        rc, out, err = run_news(capsys, monkeypatch, root, *argv)
    assert (rc, err) == (1, MISSING)
    assert not path.exists()
    assert not list(path.parent.glob("agentic.db*"))


# --- read-only guarantees ----------------------------------------------------------------------

def _prepare_readonly_root(tmp_path):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True), ("off", "web", OK_URL + "2", False)))
    (root / "logs").mkdir()
    (root / "logs" / "activity.log").write_text("2026-10-25T00:00:00+00:00\tNEWS\tcollected\tx\t-\n", encoding="utf-8")
    (root / "data" / "rag").mkdir()
    (root / "data" / "rag" / "index.bin").write_bytes(b"\x00\x01rag")
    return root


@pytest.mark.parametrize("argv", [("list",), ("test", "src"), ("test", "off")])
def test_list_and_test_leave_the_whole_tree_byte_identical_without_sidecars(tmp_path, monkeypatch, capsys, argv):
    root = _prepare_readonly_root(tmp_path)
    FakeHttp(monkeypatch)
    before = tree_snapshot(root)
    rc, _, err = run_news(capsys, monkeypatch, root, *argv)
    assert (rc, err) == (0, "")
    assert tree_snapshot(root) == before
    assert not list((root / "data").glob("agentic.db-*"))


@pytest.mark.parametrize("argv", [("list",), ("test", "src")])
def test_list_and_test_keep_every_table_and_both_logs_unchanged_with_sidecars(tmp_path, monkeypatch, capsys, argv):
    root = _prepare_readonly_root(tmp_path)
    add_sidecars(db_path(root))
    FakeHttp(monkeypatch)
    tables = dump_tables(root)
    activity = (root / "logs" / "activity.log").read_bytes()
    rag = (root / "data" / "rag" / "index.bin").read_bytes()
    rc, _, _ = run_news(capsys, monkeypatch, root, *argv)
    assert rc == 0
    assert dump_tables(root) == tables
    assert (root / "logs" / "activity.log").read_bytes() == activity
    assert (root / "data" / "rag" / "index.bin").read_bytes() == rag


def test_readonly_commands_use_the_observer_connection_and_changes_use_the_rw_connection(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    FakeHttp(monkeypatch)
    used = []
    for attr in ("connect_observer_readonly", "connect_existing_rw"):
        original = getattr(news_cli, attr)
        monkeypatch.setattr(news_cli, attr, lambda path, _o=original, _a=attr: (used.append(_a), _o(path))[1])
    for argv in (("list",), ("test", "src"), ("enable", "src"), ("disable", "src"),
                 ("add", "other", "https://example.invalid/o.xml")):
        used.clear()
        rc, _, _ = run_news(capsys, monkeypatch, root, *argv)
        assert rc == 0, argv
        expected = "connect_observer_readonly" if argv[0] in {"list", "test"} else "connect_existing_rw"
        assert used == [expected], argv


@pytest.mark.parametrize("argv", [("list",), ("enable", "src"), ("disable", "src")])
def test_commands_that_need_no_settings_work_with_broken_settings(tmp_path, monkeypatch, capsys, argv):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),), settings="broken")
    rc, _, err = run_news(capsys, monkeypatch, root, *argv)
    assert (rc, err) == (0, "")


@pytest.mark.parametrize("settings", ["broken", "missing"])
def test_add_and_test_report_unreadable_settings_with_a_fixed_word(tmp_path, monkeypatch, capsys, settings):
    root = make_root(tmp_path, sources=SOURCES, settings=settings)
    monkeypatch.setattr(news_probe, "probe", lambda *a, **k: pytest.fail("fetched"))
    for argv in (("test", "src"), ("add", "fresh", OK_URL + "-new")):
        rc, out, err = run_news(capsys, monkeypatch, root, *argv)
        assert (rc, out, err) == (1, "", "設定を読めません。\n")
    assert [row[1] for row in dump_tables(root)["news_sources"]] == ["src"]


def test_settings_are_read_after_loading_the_env_file_for_add_and_test_only(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    FakeHttp(monkeypatch)
    order = []
    real_env, real_settings = news_cli.load_env_file, news_cli.load_settings
    monkeypatch.setattr(news_cli, "load_env_file", lambda path: (order.append(("env", path)), real_env(path))[1])
    monkeypatch.setattr(news_cli, "load_settings", lambda path: (order.append(("settings", path)), real_settings(path))[1])
    run_news(capsys, monkeypatch, root, "list")
    run_news(capsys, monkeypatch, root, "enable", "src")
    assert order == []
    run_news(capsys, monkeypatch, root, "test", "src")
    assert order == [("env", root / ".env"), ("settings", root / "config" / "settings.yaml")]


# --- list ---------------------------------------------------------------------------------------

def test_list_without_sources(tmp_path, monkeypatch, capsys):
    assert run_news(capsys, monkeypatch, make_root(tmp_path), "list") == (0, "ニュース源はありません。\n", "")


def test_list_prints_one_line_and_a_folded_url_per_source(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(
        ("alpha", "feed", "https://Example.invalid:8443/p/q?k=zzz", True),
        ("beta-long-name", "web", "http://example.invalid/x", False),
        ("fresh", "feed", "https://[2001:db8::1]/f?k=zzz", True)))
    conn = connect(db_path(root))
    for name, outcome in (("alpha", "ok"), ("beta-long-name", "failure")):
        current = news_health.read(conn, name).record
        record = news_health.next_record(current, outcome, NOW, count=12 if outcome == "ok" else None,
                                         reason="HTTP 404" if outcome == "failure" else None)
        news_health.write_if_unchanged(conn, name, None, record, NOW)
    conn.close()
    monkeypatch.setattr(news_cli, "_now", lambda: NOW)
    rc, out, err = run_news(capsys, monkeypatch, root, "list")
    assert (rc, err) == (0, "")
    assert out.splitlines() == [
        "ニュース源: 3 件 (有効 2 / 無効 1)",
        "alpha           有効  feed  直近: 成功 12 件       2026-10-25 09:30 UTC",
        "  https://example.invalid:8443/…",
        "beta-long-name  無効  web   直近: 失敗 (HTTP 404)  2026-10-25 09:30 UTC",
        "  http://example.invalid/…",
        "fresh           有効  feed  直近: 未取得",
        "  https://[2001:db8::1]/…",
    ]


def _write_health(root, name, sequence, *, retry_after=None):
    conn = connect(db_path(root))
    raw = None
    for outcome in sequence:
        current = news_health.read(conn, name)
        record = news_health.next_record(current.record, outcome, NOW, count=0 if outcome == "ok" else None,
                                         reason="HTTP 503" if outcome != "ok" else None,
                                         retry_after=retry_after)
        news_health.write_if_unchanged(conn, name, current.raw, record, NOW)
    conn.close()


@pytest.mark.parametrize(("sequence", "tail"), [
    (("failure", "failure", "failure"), "連続 3 回、2026-10-25 10:30 UTC まで待機 (連続失敗)"),
    (("refusal", "refusal", "refusal"), "連続 3 回、2026-10-25 10:30 UTC まで待機 (先方の拒否が続く)"),
    (("failure", "failure"), ""),
])
def test_list_shows_the_wait_with_its_cause_label(tmp_path, monkeypatch, capsys, sequence, tail):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),))
    _write_health(root, "src", sequence)
    monkeypatch.setattr(news_cli, "_now", lambda: NOW)
    _, out, _ = run_news(capsys, monkeypatch, root, "list")
    line = out.splitlines()[1]
    assert line.endswith(("2026-10-25 09:30 UTC  " + tail) if tail else "2026-10-25 09:30 UTC")


def test_list_shows_a_directive_wait_and_a_streak_after_the_wait_expired(tmp_path, monkeypatch, capsys):
    from datetime import timedelta
    from agentic_fx.store import fetch_attempts
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True), ("old", "feed", OK_URL + "o", True)))
    _write_health(root, "old", ("failure", "failure", "failure"))
    conn = connect(db_path(root))
    fetch_attempts.record_attempt(conn, fetch_attempts.news_source_key("src"), NOW)
    fetch_attempts.record_retry_after(conn, fetch_attempts.news_source_key("src"), NOW, timedelta(hours=3))
    conn.close()
    monkeypatch.setattr(news_cli, "_now", lambda: NOW + timedelta(hours=2))
    _, out, _ = run_news(capsys, monkeypatch, root, "list")
    lines = out.splitlines()
    assert "2026-10-25 12:30 UTC まで待機 (先方の指示)" in lines[1]
    assert lines[3].endswith("連続 3 回")


def test_list_marks_a_corrupt_health_record_unreadable_without_failing(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),))
    conn = connect(db_path(root))
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                 (news_health.PREFIX + "src", "{not json", NOW.isoformat()))
    conn.commit()
    conn.close()
    rc, out, _ = run_news(capsys, monkeypatch, root, "list")
    assert rc == 0 and "直近: 読めません" in out


def test_list_survives_a_stored_url_that_cannot_be_folded_and_a_control_name(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    conn = connect(db_path(root))
    conn.execute("INSERT INTO news_sources (name,fetcher,url,enabled,added_by,created_at) VALUES (?,?,?,?,?,?)",
                 ("Bad\x1b[31mName", "feed", "not a url", 1, "user", NOW.isoformat()))
    conn.commit()
    conn.close()
    rc, out, _ = run_news(capsys, monkeypatch, root, "list")
    assert rc == 0
    assert "\x1b" not in out and "not a url" not in out


# --- add: input, order and rc table -------------------------------------------------------------

@pytest.mark.parametrize("length", [39, 40, 41])
def test_name_length_boundary(tmp_path, monkeypatch, capsys, length):
    root = make_root(tmp_path)
    http = FakeHttp(monkeypatch)
    rc, _, err = run_news(capsys, monkeypatch, root, "add", "a" * length, OK_URL)
    assert (rc, http.count) == ((0, 1) if length <= 40 else (2, 0))
    assert err == ("" if length <= 40 else "名前の形式が正しくありません\n")


@pytest.mark.parametrize("name", ["", "-", "A", "a_b", "a b", "あ", "a\x07"])
def test_name_grammar_violations_return_two_before_anything_is_touched(tmp_path, monkeypatch, capsys, name):
    root = make_root(tmp_path)
    http = FakeHttp(monkeypatch)
    tables = dump_tables(root)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", name, OK_URL)
    assert (rc, out, err, http.count) == (2, "", "名前の形式が正しくありません\n", 0)
    assert dump_tables(root) == tables


def _url_of_length(length: int) -> str:
    prefix = "https://example.invalid/"
    return prefix + "a" * (length - len(prefix))


@pytest.mark.parametrize(("url", "rc"), [
    (_url_of_length(2047), 0), (_url_of_length(2048), 0), (_url_of_length(2049), 2),
    ("ftp://example.invalid/x", 2), ("https:///x", 2), ("https://user:pw@example.invalid/x", 2),
    ("https://user@example.invalid/x", 2), ("https://example.invalid/x#frag", 2),
    ("https://example.invalid/x#", 2), ("https://example.invalid/a b", 2),
    ("https://example.invalid/a\tb", 2), ("https://example.invalid/a\x7fb", 2),
    ("example.invalid/x", 2), ("https://example.invalid:99999/x", 2),
])
def test_url_grammar_boundaries_through_the_cli(tmp_path, monkeypatch, capsys, url, rc):
    root = make_root(tmp_path)
    http = FakeHttp(monkeypatch)
    got, _, err = run_news(capsys, monkeypatch, root, "add", "src", url)
    assert got == rc
    assert http.count == (1 if rc == 0 else 0)
    if rc == 2:
        assert err == "URL の形式が正しくありません\n"
        assert dump_tables(root)["news_sources"] == []


def test_invalid_fetcher_is_an_argparse_error(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    with pytest.raises(SystemExit) as raised:
        run_news(capsys, monkeypatch, root, "add", "src", OK_URL, "--fetcher", "rss")
    assert raised.value.code == 2


def test_add_stores_an_enabled_user_source_with_the_normalised_url_and_writes_activity(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    http = FakeHttp(monkeypatch)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", "src", "HTTPS://Example.INVALID:443/rss.xml?a=B")
    assert (rc, err) == (0, "")
    assert out == ("取得: 2 件 (うち本文・要約あり 1 件)\n追加しました: src (feed)\n"
                   "反映は次の収集周期 (最長 30 分) です。\n")
    rows = dump_tables(root)["news_sources"]
    assert [(r[1], r[2], r[3], r[4], r[5]) for r in rows] == [
        ("src", "feed", "https://example.invalid/rss.xml?a=B", 1, "user")]
    assert http.count == 1
    assert http.requests[0].url.host == "example.invalid"
    lines = _activity(root)
    assert len(lines) == 1 and lines[0].split("\t")[1:4] == ["NEWS", "news_source_added", "src (feed)"]


def test_add_with_the_web_fetcher_records_the_method(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    monkeypatch.setattr(news_probe, "probe", lambda fetcher, url, name, *, timeout_sec: news_probe.ProbeResult(1, 1))
    rc, out, _ = run_news(capsys, monkeypatch, root, "add", "page", OK_URL, "--fetcher", "web")
    assert rc == 0 and "追加しました: page (web)" in out
    assert dump_tables(root)["news_sources"][0][2] == "web"


def test_add_passes_the_settings_timeout_to_the_single_fetch(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    seen = []
    monkeypatch.setattr(news_probe, "probe", lambda fetcher, url, name, *, timeout_sec:
                        (seen.append((fetcher, url, name, timeout_sec)), news_probe.ProbeResult(1, 1))[1])
    run_news(capsys, monkeypatch, root, "add", "src", "https://Example.invalid:443/x")
    assert seen == [("feed", "https://example.invalid/x", "src", 30.0)]


@pytest.mark.parametrize(("existing", "attempt_name", "attempt_url", "message"), [
    ("src", "src", "https://example.invalid/other", "名前 src は既に使われています。\n"),
    ("src", "fresh", "HTTPS://EXAMPLE.invalid:443/rss.xml", "URL が源 src と重複\n"),
    ("src", "fresh", "https://example.invalid:443/rss.xml", "URL が源 src と重複\n"),
])
def test_duplicates_are_refused_before_any_fetch(tmp_path, monkeypatch, capsys, existing, attempt_name,
                                                 attempt_url, message):
    root = make_root(tmp_path, sources=((existing, "feed", OK_URL, False),))
    http = FakeHttp(monkeypatch)
    tables = dump_tables(root)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", attempt_name, attempt_url)
    assert (rc, out, err, http.count) == (1, "", message, 0)
    assert dump_tables(root) == tables


def test_a_failed_fetch_is_a_fixed_word_and_leaves_the_database_alone(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    tables = dump_tables(root)
    cases = [(dict(status=404), "HTTP 404"), (dict(raises=__import__("httpx").ConnectError("boom https://x.invalid/k")), "接続できません"),
             (dict(raises=__import__("httpx").ReadTimeout("slow")), "タイムアウト"),
             (dict(raises=RuntimeError("weird https://x.invalid/k")), "取得できません")]
    for kwargs, word in cases:
        http = FakeHttp(monkeypatch, **kwargs)
        rc, out, err = run_news(capsys, monkeypatch, root, "add", "src", OK_URL, "--yes")
        assert (rc, out, err) == (1, "", f"取得に失敗したので追加しません: {word}\n")
        assert http.count == 1
    assert dump_tables(root) == tables
    assert _activity(root) == []


def test_one_or_more_articles_are_added_without_a_question(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    FakeHttp(monkeypatch)
    stdin = FakeStdin("", tty=True)
    rc, out, _ = run_news(capsys, monkeypatch, root, "add", "src", OK_URL, stdin=stdin)
    assert rc == 0 and "それでも" not in out


def test_zero_articles_ask_on_a_terminal_and_only_y_adds(tmp_path, monkeypatch, capsys):
    for answer, added in (("y\n", True), ("Y\n", False), ("yes\n", False), ("n\n", False), ("\n", False), ("", False)):
        root = make_root(_fresh(tmp_path, f"{answer!r}"))
        FakeHttp(monkeypatch, body=RSS_EMPTY)
        rc, out, err = run_news(capsys, monkeypatch, root, "add", "src", OK_URL, stdin=FakeStdin(answer, tty=True))
        assert out.startswith("取得: 0 件 (うち本文・要約あり 0 件)\n0 件でした。それでも追加しますか? [y/N] ")
        assert bool(dump_tables(root)["news_sources"]) is added, answer
        assert rc == (0 if added else 1), answer
        if added:
            assert "`--fetcher web` を試せます。" in out
        else:
            assert err == "追加しませんでした。\n"


def test_zero_articles_without_a_terminal_need_yes(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    FakeHttp(monkeypatch, body=RSS_EMPTY)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", "src", OK_URL, stdin=FakeStdin("y\n"))
    assert (rc, "それでも" in out) == (1, False)
    assert err == "0 件のため追加しません。追加するには --yes を付けてください。\n"
    assert dump_tables(root)["news_sources"] == []
    rc, out, _ = run_news(capsys, monkeypatch, root, "add", "src", OK_URL, "--yes", stdin=FakeStdin(""))
    assert rc == 0 and len(dump_tables(root)["news_sources"]) == 1


def test_yes_skips_the_question_but_not_a_failure(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    FakeHttp(monkeypatch, body=RSS_EMPTY)
    rc, out, _ = run_news(capsys, monkeypatch, root, "add", "src", OK_URL, "--yes", stdin=FakeStdin("", tty=True))
    assert rc == 0 and "それでも" not in out
    FakeHttp(monkeypatch, status=500)
    rc, _, err = run_news(capsys, monkeypatch, root, "add", "other", OK_URL + "2", "--yes", stdin=FakeStdin("", tty=True))
    assert (rc, err) == (1, "取得に失敗したので追加しません: HTTP 500\n")


def test_the_web_hint_is_only_for_a_zero_article_feed(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    monkeypatch.setattr(news_probe, "probe", lambda *a, **k: news_probe.ProbeResult(0, 0))
    _, out, _ = run_news(capsys, monkeypatch, root, "add", "a", OK_URL, "--yes")
    assert "`--fetcher web` を試せます。" in out
    _, out, _ = run_news(capsys, monkeypatch, root, "add", "b", OK_URL + "b", "--yes", "--fetcher", "web")
    assert "試せます" not in out


# --- add with '-' -------------------------------------------------------------------------------

def test_dash_reads_the_url_from_a_pipe(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    http = FakeHttp(monkeypatch)
    rc, out, _ = run_news(capsys, monkeypatch, root, "add", "src", "-", stdin=FakeStdin(OK_URL + "\n"))
    assert rc == 0 and http.count == 1
    assert dump_tables(root)["news_sources"][0][3] == OK_URL


def test_dash_on_a_terminal_uses_the_hidden_prompt(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    FakeHttp(monkeypatch)
    prompts = []
    monkeypatch.setattr(news_cli.getpass, "getpass", lambda prompt="": (prompts.append(prompt), OK_URL)[1])
    rc, _, _ = run_news(capsys, monkeypatch, root, "add", "src", "-", stdin=FakeStdin("never read", tty=True))
    assert rc == 0 and len(prompts) == 1


@pytest.mark.parametrize("text", ["", "\n", "   \n", OK_URL + "\nsecond\n", "a" * 2049, "a" * 2049 + "\n"])
def test_dash_input_mistakes_return_two_without_touching_anything(tmp_path, monkeypatch, capsys, text):
    root = make_root(tmp_path)
    http = FakeHttp(monkeypatch)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", "src", "-", stdin=FakeStdin(text))
    assert (rc, out, err, http.count) == (2, "", "標準入力の URL が正しくありません\n", 0)
    assert dump_tables(root)["news_sources"] == []


def test_dash_input_mistakes_on_a_terminal_return_two(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    for typed in ("", "x" * 2049):
        monkeypatch.setattr(news_cli.getpass, "getpass", lambda prompt="", _t=typed: _t)
        rc, _, err = run_news(capsys, monkeypatch, root, "add", "src", "-", stdin=FakeStdin("", tty=True))
        assert (rc, err) == (2, "標準入力の URL が正しくありません\n")


def test_dash_never_asks_and_needs_yes_for_zero_articles(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    FakeHttp(monkeypatch, body=RSS_EMPTY)
    monkeypatch.setattr(news_cli.getpass, "getpass", lambda prompt="": OK_URL)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", "src", "-", stdin=FakeStdin("y\n", tty=True))
    assert rc == 1 and "それでも" not in out and "--yes" in err
    rc, _, _ = run_news(capsys, monkeypatch, root, "add", "src", "-", "--yes", stdin=FakeStdin("", tty=True))
    assert rc == 0


def test_dash_input_is_checked_before_the_database_is_consulted(tmp_path, monkeypatch, capsys):
    root = tmp_path / "bare"
    root.mkdir()
    rc, _, err = run_news(capsys, monkeypatch, root, "add", "src", "-", stdin=FakeStdin(""))
    assert (rc, err) == (2, "標準入力の URL が正しくありません\n")
    rc, _, err = run_news(capsys, monkeypatch, root, "add", "Bad Name", OK_URL)
    assert (rc, err) == (2, "名前の形式が正しくありません\n")


# --- enable / disable ---------------------------------------------------------------------------

def test_enable_flips_the_row_initialises_health_and_writes_one_activity_line(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    _write_health(root, "src", ("failure", "failure", "failure"))
    conn = connect(db_path(root))
    from datetime import timedelta
    from agentic_fx.store import fetch_attempts
    fetch_attempts.record_attempt(conn, fetch_attempts.news_source_key("src"), NOW)
    fetch_attempts.record_retry_after(conn, fetch_attempts.news_source_key("src"), NOW, timedelta(hours=3))
    attempts_before = conn.execute("SELECT key,value FROM alert_state WHERE key LIKE ?",
                                   (fetch_attempts.news_source_key("src") + "%",)).fetchall()
    conn.close()
    rc, out, err = run_news(capsys, monkeypatch, root, "enable", "src")
    assert (rc, err) == (0, "")
    assert out == "有効にしました: src\n反映は次の収集周期 (最長 30 分) です。\n"
    conn = connect(db_path(root))
    try:
        assert news_sources.list_all(conn)[0]["enabled"] == 1
        record = news_health.read(conn, "src").record
        assert (record.consecutive_failures, record.retry_after_streak, record.backoff_until) == (0, 0, None)
        assert record.reset_at is not None and record.reset_nonce
        assert conn.execute("SELECT key,value FROM alert_state WHERE key LIKE ?",
                            (fetch_attempts.news_source_key("src") + "%",)).fetchall() == attempts_before
    finally:
        conn.close()
    assert [line.split("\t")[1:4] for line in _activity(root)] == [["NEWS", "news_source_enabled", "src"]]


def test_disable_flips_the_row_and_keeps_the_health_record(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),))
    _write_health(root, "src", ("failure", "failure", "failure"))
    raw = dump_tables(root)["alert_state"]
    rc, out, _ = run_news(capsys, monkeypatch, root, "disable", "src")
    assert rc == 0 and out == "無効にしました: src\n反映は次の収集周期 (最長 30 分) です。\n"
    tables = dump_tables(root)
    assert tables["alert_state"] == raw and tables["news_sources"][0][4] == 0
    assert [line.split("\t")[1:4] for line in _activity(root)] == [["NEWS", "news_source_disabled", "src"]]


@pytest.mark.parametrize(("command", "enabled", "word"), [("enable", True, "既に有効です"), ("disable", False, "既に無効です")])
def test_repeating_a_change_is_a_silent_success(tmp_path, monkeypatch, capsys, command, enabled, word):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, enabled),))
    _write_health(root, "src", ("failure", "failure", "failure"))
    tables = dump_tables(root)
    rc, out, err = run_news(capsys, monkeypatch, root, command, "src")
    assert (rc, out, err) == (0, word + "\n", "")
    assert dump_tables(root) == tables
    assert _activity(root) == []


@pytest.mark.parametrize("command", ["enable", "disable", "test"])
def test_unknown_source_is_one_and_control_characters_are_two(tmp_path, monkeypatch, capsys, command):
    root = make_root(tmp_path, sources=SOURCES)
    FakeHttp(monkeypatch)
    rc, out, err = run_news(capsys, monkeypatch, root, command, "nope")
    assert (rc, out, err) == (1, "", "ニュース源 nope はありません。\n")
    rc, out, err = run_news(capsys, monkeypatch, root, command, "a\x1bb")
    assert (rc, out, err) == (2, "", "名前の形式が正しくありません\n")


def test_sources_with_names_outside_the_grammar_can_be_operated(tmp_path, monkeypatch, capsys):
    long_name = "Z" * 60
    root = make_root(tmp_path, sources=(("Upper_Case", "feed", OK_URL, True), (long_name, "feed", OK_URL + "l", False)))
    FakeHttp(monkeypatch)
    assert run_news(capsys, monkeypatch, root, "disable", "Upper_Case")[0] == 0
    assert run_news(capsys, monkeypatch, root, "enable", long_name)[0] == 0
    assert run_news(capsys, monkeypatch, root, "test", "Upper_Case")[0] == 0
    _, out, _ = run_news(capsys, monkeypatch, root, "list")
    assert "Upper_Case" in out and long_name in out
    conn = connect(db_path(root))
    try:
        assert news_health.read(conn, long_name).record.reset_nonce
    finally:
        conn.close()


def test_enable_is_one_transaction_that_rolls_back_the_row_when_health_fails(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    tables = dump_tables(root)

    def broken(conn, name, now, **kwargs):
        raise RuntimeError("fault https://example.invalid/secret?k=zzz")

    monkeypatch.setattr(news_health, "initialize_for_enable", broken)
    rc, out, err = run_news(capsys, monkeypatch, root, "enable", "src")
    assert (rc, out, err) == (1, "", "予期しない失敗のため中止しました。\n")
    assert dump_tables(root) == tables
    assert _activity(root) == []


def test_enable_rolls_back_the_health_record_when_the_commit_fails(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    tables = dump_tables(root)

    class Failing:
        def __init__(self, conn):
            self._conn = conn

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    original = news_cli.connect_existing_rw
    monkeypatch.setattr(news_cli, "connect_existing_rw", lambda path: Failing(original(path)))
    rc, _, err = run_news(capsys, monkeypatch, root, "enable", "src")
    assert (rc, err) == (1, "DB を操作できません。\n")
    assert dump_tables(root) == tables


def test_a_failing_activity_write_does_not_undo_the_change(tmp_path, monkeypatch, capsys, caplog):
    root = make_root(tmp_path, sources=SOURCES)
    (root / "logs").mkdir()
    (root / "logs" / "activity.log").mkdir()   # appending to a directory fails
    with caplog.at_level("WARNING"):
        rc, out, err = run_news(capsys, monkeypatch, root, "enable", "src")
    assert (rc, err) == (0, "")
    assert "有効にしました: src" in out
    assert dump_tables(root)["news_sources"][0][4] == 1
    assert any("activity write failed" in record.getMessage() for record in caplog.records)
    FakeHttp(monkeypatch)
    rc, _, _ = run_news(capsys, monkeypatch, root, "add", "second", OK_URL + "2")
    assert rc == 0 and len(dump_tables(root)["news_sources"]) == 2


# --- test ---------------------------------------------------------------------------------------

def test_test_prints_the_counts_and_writes_nothing_even_for_a_disabled_source(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    http = FakeHttp(monkeypatch)
    tables = dump_tables(root)
    rc, out, err = run_news(capsys, monkeypatch, root, "test", "src")
    assert (rc, out, err, http.count) == (0, "取得: 2 件 (うち本文・要約あり 1 件)\n", "", 1)
    assert dump_tables(root) == tables
    assert _activity(root) == []


def test_test_reports_a_failure_as_a_fixed_word(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=SOURCES)
    FakeHttp(monkeypatch, status=503)
    rc, out, err = run_news(capsys, monkeypatch, root, "test", "src")
    assert (rc, out, err) == (1, "", "取得に失敗しました: HTTP 503\n")


def test_test_announces_a_wait_but_still_fetches_once(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),))
    _write_health(root, "src", ("refusal", "refusal", "refusal"))
    http = FakeHttp(monkeypatch)
    monkeypatch.setattr(news_cli, "_now", lambda: NOW)
    rc, out, _ = run_news(capsys, monkeypatch, root, "test", "src")
    assert rc == 0 and http.count == 1
    assert out.splitlines()[0] == ("この源は 2026-10-25 10:30 UTC まで待機中です"
                                   " (先方の拒否が続く、収集器は取得しません)。")


def test_test_with_an_unknown_stored_fetcher_is_a_fixed_failure(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    conn = connect(db_path(root))
    conn.execute("INSERT INTO news_sources (name,fetcher,url,enabled,added_by,created_at) VALUES (?,?,?,?,?,?)",
                 ("odd", "ftp", OK_URL, 1, "user", NOW.isoformat()))
    conn.commit()
    conn.close()
    rc, _, err = run_news(capsys, monkeypatch, root, "test", "odd")
    assert (rc, err) == (1, "取得に失敗しました: 取得できません\n")

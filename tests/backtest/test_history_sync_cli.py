"""afx history sync / import の稼働中拒否、gaps・conflicts の読み取りと解決の配線。"""
from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agentic_fx.backtest.cli as cli
from agentic_fx.backtest import history_sync, history_sync_cli
from agentic_fx.store import history_conflicts as hc
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.instance_lock import acquire_instance_lock
from tests.backtest.history_sync_support import Clock, FakeBridge, SYMBOL, T, seed

REPO = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli.register_subparsers(sub)
    return parser


def _args(*argv):
    return _parser().parse_args(["history", *argv])


def _root(tmp_path, *, source="mt5", interval="15m", months=1):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    text = (REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8")
    text = text.replace('eval_source: "none"', f'eval_source: "{source}"', 1)
    text = text.replace('base_interval: "1m"', f'base_interval: "{interval}"', 1)
    text = text.replace("holdout_months: 3", f"holdout_months: {months}", 1)
    (root / "config" / "settings.yaml").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def no_init(monkeypatch):
    monkeypatch.setattr(cli, "ensure_initialized", lambda _root: None)


def _boom(*_a, **_k):
    pytest.fail("must not be reached")


IMPORT_ARGV = ["import", "--symbol", "USDJPY", "--from", "2026-11-02", "--to", "2026-11-03",
               "--interval", "1m"]


# ---- import の稼働中拒否 ----------------------------------------------------

@pytest.mark.parametrize("source", ["mt5", "dukascopy"])
def test_import_while_the_instance_lock_is_held_is_rc_1_and_touches_nothing(
        tmp_path, no_init, monkeypatch, capsys, source):
    root = _root(tmp_path)
    holder = acquire_instance_lock(root / "data")
    try:
        for name in ("import_mt5", "import_dukascopy", "_history_import", "connect"):
            monkeypatch.setattr(cli, name, _boom)
        monkeypatch.setattr(history_sync_cli, "connect", _boom)
        rc = cli.dispatch(_args(*IMPORT_ARGV, "--source", source), root)
    finally:
        holder.close()
    err = capsys.readouterr().err
    assert rc == 1 and "instance_locked" in err
    assert "daemon" in err and "kill switch" in err
    assert not (root / "data" / "agentic.db").exists()          # DB を開いても作ってもいない


def test_import_takes_the_instance_lock_before_it_opens_the_database_and_releases_it(
        tmp_path, no_init, monkeypatch):
    root = _root(tmp_path)
    events = []
    real_acquire = history_sync_cli.acquire_instance_lock
    real_connect = history_sync_cli.connect

    def acquire(path):
        fh = real_acquire(path)
        events.append("lock")
        return fh

    def open_db(path):
        events.append("connect")
        return real_connect(path)

    def import_stub(conn, settings, args):
        events.append("import")
        with pytest.raises(history_sync_cli.InstanceAlreadyRunning):
            real_acquire(root / "data")                         # import 中は保持している
        return 0
    monkeypatch.setattr(history_sync_cli, "acquire_instance_lock", acquire)
    monkeypatch.setattr(history_sync_cli, "connect", open_db)
    monkeypatch.setattr(cli, "connect", open_db)
    monkeypatch.setattr(cli, "_history_import", import_stub)
    assert cli.dispatch(_args(*IMPORT_ARGV, "--source", "mt5"), root) == 0
    assert events == ["lock", "connect", "import"]
    real_acquire(root / "data").close()                         # 終了後は解放されている


def test_import_validation_errors_still_come_after_the_lock_and_release_it(
        tmp_path, no_init, capsys):
    root = _root(tmp_path)
    rc = cli.dispatch(_args("import", "--source", "dukascopy", "--symbol", "USDJPY",
                            "--from", "2026-11-03", "--to", "2026-11-02"), root)
    assert rc == 2
    acquire_instance_lock(root / "data").close()


# ---- sync -----------------------------------------------------------------

def test_sync_while_the_instance_lock_is_held_is_rc_1_with_no_database_and_no_bridge(
        tmp_path, no_init, monkeypatch, capsys):
    root = _root(tmp_path)
    holder = acquire_instance_lock(root / "data")
    try:
        monkeypatch.setattr(history_sync_cli, "connect", _boom)
        monkeypatch.setattr(history_sync, "run_sync", _boom)
        monkeypatch.setattr(history_sync, "_default_fetch", _boom)
        rc = cli.dispatch(_args("sync"), root)
    finally:
        holder.close()
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("instance_locked")
    assert not (root / "data" / "agentic.db").exists()


def test_sync_with_a_non_mt5_source_is_rc_2_before_any_lock_or_io(
        tmp_path, no_init, monkeypatch, capsys):
    root = _root(tmp_path, source="dukascopy")
    monkeypatch.setattr(history_sync_cli, "acquire_instance_lock", _boom)
    monkeypatch.setattr(history_sync_cli, "connect", _boom)
    assert cli.dispatch(_args("sync"), root) == 2
    assert "not_applicable_eval_source" in capsys.readouterr().err
    assert not (root / "data" / "instance.lock").exists()


def test_sync_runs_under_the_lock_prints_the_result_and_releases_the_lock(
        tmp_path, no_init, monkeypatch, capsys):
    root = _root(tmp_path)
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    from agentic_fx.backtest.holdout import in_sample_until
    seed(conn, SYMBOL, in_sample_until(T, 1, base_interval="15m"),
         datetime(2026, 11, 11, 11, 30, tzinfo=UTC))
    conn.close()
    clock = Clock()
    real = history_sync.run_sync

    def run(conn, settings, **kw):
        with pytest.raises(history_sync_cli.InstanceAlreadyRunning):
            acquire_instance_lock(root / "data")                 # 実行中は保持している
        return real(conn, settings, fetch=FakeBridge(), monotonic=clock.monotonic,
                    sleep=clock.sleep, **kw)
    monkeypatch.setattr(history_sync, "run_sync", run)
    monkeypatch.setattr(history_sync_cli, "_utc_now", lambda: T)
    rc = cli.dispatch(_args("sync", "--until-done"), root)
    out = capsys.readouterr().out
    assert rc == 0
    assert "result=ok rc=0" in out and "cursor symbol=USDJPY" in out
    acquire_instance_lock(root / "data").close()


def test_sync_rc_zero_deferral_still_prints_reason_range_cursor_and_next(
        tmp_path, no_init, monkeypatch, capsys):
    root = _root(tmp_path)
    clock = Clock()
    real = history_sync.run_sync
    # 比較できる既存の足が無い (空 DB) が gap にならないよう、gap の判定を空にして deferral を単独で出す
    monkeypatch.setattr(history_sync, "guard_relevant", lambda missing, width: {})
    monkeypatch.setattr(history_sync, "run_sync", lambda conn, settings, **kw: real(
        conn, settings, fetch=FakeBridge(), monotonic=clock.monotonic, sleep=clock.sleep, **kw))
    monkeypatch.setattr(history_sync_cli, "_utc_now", lambda: T)
    rc = cli.dispatch(_args("sync"), root)
    out = capsys.readouterr().out
    assert rc == 0 and "result=deferred rc=0" in out
    assert "deferred reason=inspection_insufficient" in out
    assert "remaining=USDJPY [" in out and "last_processed_e=uninitialized" in out
    assert "next:" in out


# ---- 読み取り専用 ---------------------------------------------------------

def _populate(root):
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    from agentic_fx.store import history_sync as store
    store.write_txn(conn, lambda c: [
        store.observe_gap(c, source="mt5", interval="15m", symbol="USDJPY",
                          day_utc="2026-10-20", kind="primary_exact", missing_count=90,
                          run_id=None, now=T),
        store.observe_gap(c, source="mt5", interval="15m", symbol="USDJPY",
                          day_utc="2026-10-05", kind="primary_exact", missing_count=90,
                          run_id=None, now=T),
        c.execute("UPDATE history_sync_gaps SET state='expired' WHERE day_utc='2026-10-05'")])
    conn.execute("INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, close, "
                 "volume, source) VALUES ('USDJPY','15m','2026-11-03T13:45:00+00:00',1,2,0.5,1,0,'mt5')")
    conn.commit()
    hc.upsert(conn, source="mt5", interval="15m", symbol="USDJPY",
              bar_time="2026-11-03T13:45:00+00:00", history_epoch="none",
              existing=(1, 2, 0.5, 1, 0, None), incoming=(1, 2, 0.5, 1.4, 0, None),
              origin="import", seen_at="2026-11-03T14:00:00+00:00")
    conn.commit()
    conn.close()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gaps_list_shows_open_gaps_and_a_non_actionable_expired_count(tmp_path, capsys):
    root = _root(tmp_path)
    _populate(root)
    assert cli.dispatch(_args("gaps", "list"), root) == 0
    out = capsys.readouterr().out
    assert "対象は holdout 内と直近です。in-sample は作りません。" in out
    assert "gap symbol=USDJPY day=2026-10-20 kind=primary_exact missing=90" in out
    assert "2026-10-05" not in out                             # expired は行にしない
    assert "expired: holdout 外、自動修復なし 1 日" in out
    assert "next" not in out.lower().replace("next_retry", "")


def test_readonly_commands_never_write_the_database_or_create_one(tmp_path, capsys):
    root = _root(tmp_path)
    assert cli.dispatch(_args("gaps", "list"), root) == 1
    assert "database_unavailable" in capsys.readouterr().out
    assert not (root / "data" / "agentic.db").exists()
    _populate(root)
    db = root / "data" / "agentic.db"
    before = _sha(db)
    for argv in (("gaps", "list"), ("conflicts", "list"), ("conflicts", "show", "1")):
        assert cli.dispatch(_args(*argv), root) == 0
    assert _sha(db) == before


def test_tables_missing_in_an_old_database_print_not_initialized(tmp_path, capsys):
    root = _root(tmp_path)
    import sqlite3
    conn = sqlite3.connect(root / "data" / "agentic.db")
    conn.execute("CREATE TABLE ohlcv_history (x)")
    conn.commit()
    conn.close()
    for argv in (("gaps", "list"), ("conflicts", "list"), ("conflicts", "show", "1")):
        assert cli.dispatch(_args(*argv), root) == 0
        assert "history_sync: not initialized" in capsys.readouterr().out


def test_conflicts_list_and_show_print_the_record_and_filter_by_state(tmp_path, capsys):
    root = _root(tmp_path)
    _populate(root)
    assert cli.dispatch(_args("conflicts", "list"), root) == 0
    out = capsys.readouterr().out
    assert "conflict id=1 state=open source=mt5 interval=15m symbol=USDJPY" in out
    assert cli.dispatch(_args("conflicts", "list", "--state", "kept"), root) == 0
    assert "conflict はありません" in capsys.readouterr().out
    assert cli.dispatch(_args("conflicts", "show", "1"), root) == 0
    shown = capsys.readouterr().out
    assert "existing_json:" in shown and "incoming_json:" in shown and "state: open" in shown
    assert cli.dispatch(_args("conflicts", "show", "99"), root) == 2


def test_readonly_commands_do_not_read_while_setup_or_enroll_holds_the_lock(
        tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _populate(root)
    holder = acquire_instance_lock(root / "data")
    try:
        monkeypatch.setattr(history_sync_cli, "connect_observer_readonly", _boom)
        import agentic_fx.status as status
        monkeypatch.setattr(status, "_locked_by", lambda _root: "setup")
        for argv in (("gaps", "list"), ("conflicts", "list"), ("conflicts", "show", "1")):
            assert cli.dispatch(_args(*argv), root) == 1
            assert "setup / enroll" in capsys.readouterr().out
    finally:
        holder.close()


def test_readonly_commands_still_read_while_an_unknown_holder_such_as_the_daemon_runs(
        tmp_path, capsys):
    root = _root(tmp_path)
    _populate(root)
    holder = acquire_instance_lock(root / "data")                 # marker なしの保持者 (unknown)
    try:
        assert cli.dispatch(_args("conflicts", "list"), root) == 0
        assert "conflict id=1" in capsys.readouterr().out
    finally:
        holder.close()


# ---- 解決 -----------------------------------------------------------------

def test_resolve_keep_runs_under_the_lock_and_does_not_echo_the_reason(
        tmp_path, no_init, capsys):
    root = _root(tmp_path)
    _populate(root)
    reason = "canary-reason-" + "text"
    rc = cli.dispatch(_args("conflicts", "resolve", "1", "--action", "keep-existing",
                            "--reason", reason), root)
    captured = capsys.readouterr()
    assert rc == 0 and "resolved id=1 action=keep-existing state=kept" in captured.out
    assert reason not in captured.out and reason not in captured.err
    acquire_instance_lock(root / "data").close()


def test_resolve_replace_prints_backup_revision_and_the_epoch_warning(tmp_path, no_init, capsys):
    root = _root(tmp_path)
    _populate(root)
    rc = cli.dispatch(_args("conflicts", "resolve", "1", "--action", "replace-with-incoming",
                            "--reason", "確認済み"), root)
    out = capsys.readouterr().out
    assert rc == 0 and "revision=1 backup=" in out and "epoch が変わります" in out
    assert list((root / "data").glob("agentic.db.bak-history-replace-*"))


@pytest.mark.parametrize("action", ["keep-existing", "replace-with-incoming"])
def test_resolve_while_the_instance_lock_is_held_is_rc_1_and_changes_nothing(
        tmp_path, no_init, monkeypatch, capsys, action):
    root = _root(tmp_path)
    _populate(root)
    db = root / "data" / "agentic.db"
    before = _sha(db)
    holder = acquire_instance_lock(root / "data")
    try:
        monkeypatch.setattr(history_sync_cli, "connect", _boom)
        rc = cli.dispatch(_args("conflicts", "resolve", "1", "--action", action,
                                "--reason", "x"), root)
    finally:
        holder.close()
    assert rc == 1 and "instance_locked" in capsys.readouterr().err
    assert _sha(db) == before


def test_resolve_argument_errors_are_rc_2_and_unknown_actions_are_refused_by_the_parser(
        tmp_path, no_init, capsys):
    root = _root(tmp_path)
    _populate(root)
    assert cli.dispatch(_args("conflicts", "resolve", "1", "--action", "keep-existing",
                              "--reason", "  "), root) == 2
    assert cli.dispatch(_args("conflicts", "resolve", "42", "--action", "keep-existing",
                              "--reason", "x"), root) == 2
    with pytest.raises(SystemExit):
        _args("conflicts", "resolve", "1", "--action", "delete", "--reason", "x")
    with pytest.raises(SystemExit):
        _args("conflicts", "resolve", "1", "--action", "keep-existing")      # --reason は必須

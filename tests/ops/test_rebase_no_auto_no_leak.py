"""HWM の付け直しは人の明示操作だけが動かし、金額は専用の preview と DB の台帳にしか出ない。

reset・reconcile・自動の再ラッチ・起動時の回復は HWM と台帳を変えない。通常の status・
event・journal・audit に新しい金額 field も preview の中身も出ない。"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from agentic_fx.ops.contracts import Principal

from .test_rebase_service import (LOSS, START, _gen, _ledger, _post, _preview,  # noqa: F401
                                  _snapshots, rig)

APPROVER = Principal.APPROVER

PREVIEW_ONLY_WORDS = ("cumulative_cut", "additional_headroom", "past_cut", "cut_pct",
                      "new_hwm", "warning_threshold", "count_after", "source_snapshot",
                      "preview_token", "cumulative_warning")


def _dump(conn):
    return ([tuple(row) for row in _snapshots(conn)], [tuple(row) for row in _ledger(conn)])


def _rebased(rig):
    _post(rig, _preview(rig)["preview_token"])
    return _dump(rig.conn)


def test_reset_does_not_change_hwm_or_ledger(rig):
    before = _rebased(rig)
    rig.service.reset_kill_switch(APPROVER, _gen(rig))
    assert rig.state.load().kill_switch_latched is False
    assert _dump(rig.conn) == before


def test_reset_alone_keeps_the_old_hwm_so_dd_stays_over_the_threshold(rig):
    before = _dump(rig.conn)
    rig.service.reset_kill_switch(APPROVER, _gen(rig))
    assert _dump(rig.conn) == before
    assert before[0][-1][4] == START and before[0][-1][3] == START - LOSS  # hwm / equity
    assert before[1] == []


def test_shell_style_direct_reset_does_not_change_hwm_or_ledger(rig):
    before = _rebased(rig)
    rig.state.reset_kill_switch(_gen(rig))
    assert _dump(rig.conn) == before


def test_reconcile_confirm_does_not_change_hwm_or_ledger(rig):
    before = _rebased(rig)
    rig.state._write_marker(_gen(rig))
    rig.service.reconcile_kill_switch(APPROVER)
    assert rig.state.reconcile_marker() is None
    assert _dump(rig.conn) == before


def test_relatch_after_reset_and_new_generation_do_not_change_hwm_or_ledger(rig):
    before = _rebased(rig)
    rig.state.reset_kill_switch(_gen(rig))
    rig.state.update(kill_switch_latched=True)  # 自動の再ラッチと同じ書込み
    assert _gen(rig) == 2
    assert _dump(rig.conn) == before


def test_startup_recovery_does_not_change_hwm_or_ledger(rig):
    before = _rebased(rig)
    restarted = rig.env.service(rebase=rig.wiring)
    restarted.recover_after_journal()
    assert _dump(rig.conn) == before
    assert rig.state.load().kill_switch_latched is True


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """技術ログの実際の出力先を捕捉する。

    daemon の logger は `agentic_fx` (propagate なし) で、file (logs/agentic.log) と
    systemd の journal が拾う stderr の StreamHandler に出す。root へ流れないので caplog は
    これを見られない。"""
    import io
    import sys

    from agentic_fx.logging_setup import setup_technical_logging
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    log_dir = tmp_path / "technical"
    logger = setup_technical_logging(log_dir, "DEBUG", daemon=True)
    yield SimpleNamespace(stream=stream, file=log_dir / "agentic.log", logger=logger)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture
def canaries(monkeypatch):
    """実行時に組み立てる秘密 (固定の値をソースに置かない)。"""
    import secrets

    from agentic_fx.ops import keys as ops_keys
    values = {"MT5_BRIDGE_API_KEY": "bridge-" + secrets.token_hex(12),
              "TWELVEDATA_API_KEY": "td-" + secrets.token_hex(12)}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    values["ops_key"] = ops_keys.new_token()
    return list(values.values())


def _journal_text(journal):
    for handler in journal.logger.handlers:
        handler.flush()
    return journal.stream.getvalue() + "\n" + journal.file.read_text(encoding="utf-8")


def _all_text(rig, caplog, journal=None):
    parts = [rig.env.activity_path.read_text(encoding="utf-8") if rig.env.activity_path.exists()
             else ""]
    for table, columns in (("ops_requests", "*"), ("ops_events", "*")):
        for row in rig.conn.execute(f"SELECT {columns} FROM {table}").fetchall():
            parts.append(json.dumps([str(value) for value in tuple(row)], ensure_ascii=False))
    parts.append(caplog.text)
    if journal is not None:
        parts.append(_journal_text(journal))
    return "\n".join(parts)


def test_audit_event_activity_and_journal_carry_no_preview_or_amounts(rig, caplog):
    caplog.set_level(logging.DEBUG)
    view = _preview(rig)
    token = view["preview_token"]
    _post(rig, token)
    text = _all_text(rig, caplog)
    for word in PREVIEW_ONLY_WORDS:
        if word == "preview_token":
            continue  # 台帳の列。audit の本文には載せない (下で検査)
        assert word not in text, word
    for amount in (START, START - LOSS, LOSS, view["additional_headroom"]):
        for form in (str(amount), f"{amount:,.0f}", f"{amount:.2f}", str(int(amount))):
            assert form not in text, form
    # 要求本文の token は audit / event / journal / activity に残さない。
    assert token not in text
    # 台帳には token が残る (調査用)。
    assert _ledger(rig.conn)[0]["preview_token"] == token


def test_ops_terminal_response_is_the_four_ids_only(rig):
    _post(rig, _preview(rig)["preview_token"])
    rows = rig.conn.execute(
        "SELECT response_json FROM ops_requests WHERE endpoint LIKE '%rebase' "
        "AND phase != 'accepted'").fetchall()
    assert rows
    for row in rows:
        assert set(json.loads(row["response_json"])) <= {
            "rebase_id", "snapshot_id", "generation", "latched", "error"}


def test_a_failed_post_also_leaves_no_amounts(rig, caplog):
    caplog.set_level(logging.DEBUG)
    view = _preview(rig)
    with pytest.raises(Exception):
        _post(rig, "f" * 64)  # token 不一致 = preview_changed
    text = _all_text(rig, caplog)
    for amount in (START, START - LOSS, LOSS):
        assert str(amount) not in text and f"{amount:,.0f}" not in text
    assert "f" * 64 not in text
    assert view["preview_token"] not in text


def test_status_and_event_sources_know_no_rebase_field():
    """通常の status / bot / web / event を作る側は preview の field も台帳も知らない。"""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    roots = [repo / "src" / "agentic_fx" / "status.py", repo / "src" / "agentic_fx" / "ops" /
             "events.py", repo / "src" / "agentic_fx" / "ops" / "trade_events.py",
             repo / "src" / "agentic_fx" / "ops" / "bot_health.py",
             repo / "src" / "agentic_fx" / "ops" / "base_view.py"]
    roots += list((repo / "afx-ops" / "afx_ops").rglob("*.py"))
    for path in roots:
        text = path.read_text(encoding="utf-8")
        for word in PREVIEW_ONLY_WORDS + ("kill_switch_hwm_rebases", "manual_ks_rebase"):
            assert word not in text, (path.name, word)


# ------------------------------------------------------------------ 自動解除の実経路
#
# 本番の経路で latch を False にするのは明示操作の reset だけ (下の source scan)。
# 自動で解除する経路は replay の `_auto_release_kill_switch` だけで、replay 専用の DB と
# state に対して `replay_ks_rebase` の snapshot を足す。手動の台帳は触らない。

def _latch_writers(source):
    import ast
    tree = ast.parse(source)
    writers = set()
    class Scan(ast.NodeVisitor):
        scope = []
        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
        visit_AsyncFunctionDef = visit_FunctionDef
        def record(self):
            writers.add(".".join(self.scope) or "<module>")
        def visit_Call(self, node):
            for kw in node.keywords:
                if kw.arg == "kill_switch_latched":
                    self.record()
                elif kw.arg is None and isinstance(kw.value, ast.Dict):
                    if any(isinstance(k, ast.Constant) and k.value == "kill_switch_latched" for k in kw.value.keys):
                        self.record()
            self.generic_visit(node)
        def visit_Attribute(self, node):
            if isinstance(node.ctx, ast.Store) and node.attr == "kill_switch_latched":
                self.record()
            self.generic_visit(node)
        def visit_Subscript(self, node):
            if isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Constant) and node.slice.value == "kill_switch_latched":
                self.record()
            self.generic_visit(node)
    Scan().visit(tree)
    return writers


def test_latch_writer_scan_detects_new_functions_and_write_forms():
    source = """
def other(value):
    update(kill_switch_latched=value)
    update(**{"kill_switch_latched": False})
    state.kill_switch_latched = value
    state["kill_switch_latched"] = value
"""
    assert _latch_writers(source) == {"other"}
    for statement in source.splitlines()[2:]:
        assert _latch_writers("def other(value):\n" + statement + "\n") == {"other"}


def test_only_known_functions_write_latch_in_src():
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
    writers = sorted(f"{path.relative_to(src)}:{name}" for path in src.rglob("*.py")
                     for name in _latch_writers(path.read_text(encoding="utf-8")))
    assert writers == [
        "backtest/runner.py:_auto_release_kill_switch",
        "core/executor.py:Executor._evaluate_and_execute_open",
        "core/executor.py:Executor._open",
        "core/executor.py:Executor.open_from_snapshot",
        "store/state.py:GuardedState.projected",
        "store/state.py:StateStore.confirm_latched",
        "store/state.py:StateStore.load",
        "store/state.py:StateStore.reset_kill_switch",
        "store/state.py:parse_state",
    ]


def test_replay_auto_release_changes_neither_the_live_hwm_nor_the_manual_ledger(rig, tmp_path):
    from datetime import datetime, timezone

    from agentic_fx.backtest.replay import ReplayClock
    from agentic_fx.backtest.runner import (_RecordingActivity, _RecordingStateStore,
                                            _auto_release_kill_switch)
    from agentic_fx.store import snapshots
    from agentic_fx.store.db import connect, init_db

    before = _rebased(rig)  # 本番側: 台帳 1 行、snapshot 追加済み
    live_state = rig.state._path.read_bytes()

    now = datetime(2026, 9, 6, 21, tzinfo=timezone.utc)
    replay_conn = connect(tmp_path / "replay.db")
    init_db(replay_conn)
    replay_conn.execute(
        "INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
        "VALUES (?,?,?,?,?,?)", (now.isoformat(), 98_000.0, 98_000.0, 100_000.0, 0.0, "paper"))
    replay_conn.commit()
    mtm = snapshots.latest(replay_conn)
    clock = ReplayClock(now)
    replay_state = _RecordingStateStore(tmp_path / "replay_state.json", clock=clock)
    replay_state.update(kill_switch_latched=True, transition_snapshot_id=mtm["id"])
    assert _auto_release_kill_switch(
        replay_conn, state=replay_state, activity=_RecordingActivity(clock), now=now,
        mtm=mtm, latched_at=datetime(2026, 9, 4, 20, tzinfo=timezone.utc))

    # 実際に自動解除が動いた (replay の HWM は付け直され、latch は外れた)
    assert replay_state.load().kill_switch_latched is False
    assert snapshots.latest(replay_conn)["source"] == "replay_ks_rebase"
    assert snapshots.latest(replay_conn)["hwm"] == 98_000.0
    # 手動の台帳には書かない
    assert replay_conn.execute("SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0] == 0
    # 本番の HWM・台帳・state ファイルは変わらない
    assert _dump(rig.conn) == before
    assert rig.state._path.read_bytes() == live_state


def test_executor_auto_relatch_after_reset_changes_neither_hwm_nor_ledger(rig):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.core.contracts import FixedClock, Origin, TradeIntent
    from agentic_fx.core.executor import Executor
    from agentic_fx.core.notifier import Notifier
    from agentic_fx.store import missions
    from tests.core.test_executor import QUOTE, SPEC, _rate_fn

    _post(rig, _preview(rig)["preview_token"])
    rig.state.reset_kill_switch(_gen(rig))
    from .test_rebase_service import NOW
    # 付け直し後にさらに損失が出て DD が閾値を超えた状態 (通常の snapshot 記録)。
    record_snapshot(rig.conn, now=NOW, balance=START - LOSS - 120_000,
                    equity=START - LOSS - 120_000)
    before = _dump(rig.conn)
    executor = Executor(
        conn=rig.conn, broker=rig.wiring.broker, settings=rig.env.settings,
        state_store=rig.state, activity=ActivityLog(rig.env.activity_path),
        notifier=Notifier(enabled=False, webhook_url=None), clock=FixedClock(NOW),
        quote_fn=lambda pair: QUOTE, spec_fn=lambda pair: SPEC, rate_fn=_rate_fn)
    mission = missions.start(rig.conn, "trade", "local", "m", NOW)
    intent = TradeIntent.from_llm_dict(
        {"action": "open", "pair": "USDJPY", "direction": "long", "entry_type": "limit",
         "horizon": "day", "limit_price": 148.20, "expires_in": "4h", "stop_loss": 147.80,
         "take_profit": 149.00, "reasoning": "t"}, origin=Origin.SCHEDULER)
    assert executor.handle_intent(intent, mission)["result"] == "rejected"
    assert rig.state.load().kill_switch_latched is True  # 実際に自動で再ラッチした
    assert _gen(rig) == 2
    assert _dump(rig.conn) == before


def test_journal_capture_sees_the_real_output_destinations(journal):
    journal.logger.getChild("probe").warning("probe-line")
    assert "probe-line" in journal.stream.getvalue()
    assert "probe-line" in _journal_text(journal)
    assert "probe-line" in journal.file.read_text(encoding="utf-8")


def _leaks(text, view, token, canaries):
    found = [word for word in PREVIEW_ONLY_WORDS if word != "preview_token" and word in text]
    found += [c for c in canaries if c in text]
    if token in text:
        found.append("preview_token value")
    for amount in (START, START - LOSS, LOSS, view["additional_headroom"]):
        for form in (str(amount), f"{amount:,.0f}", f"{amount:.2f}", str(int(amount))):
            if form in text:
                found.append(form)
    return found


def test_success_leaves_no_preview_amount_or_secret_in_audit_event_activity_or_journal(
        rig, caplog, journal, canaries):
    caplog.set_level(logging.DEBUG)
    view = _preview(rig)
    token = view["preview_token"]
    _post(rig, token)
    rig.service.reset_kill_switch(APPROVER, _gen(rig))
    assert _leaks(_all_text(rig, caplog, journal), view, token, canaries) == []


def test_failure_leaves_no_preview_amount_or_secret_in_any_output(rig, caplog, journal,
                                                                  canaries):
    caplog.set_level(logging.DEBUG)
    view = _preview(rig)
    token = view["preview_token"]
    with pytest.raises(Exception):
        _post(rig, "f" * 64)
    assert _leaks(_all_text(rig, caplog, journal), view, token, canaries + ["f" * 64]) == []


@pytest.mark.parametrize("kind", ["bridge", "ops_key", "token", "word", "amount"])
def test_the_leak_check_itself_detects_each_kind_in_the_journal(rig, caplog, journal,
                                                               canaries, kind):
    view = _preview(rig)
    token = view["preview_token"]
    planted = {"bridge": canaries[0], "ops_key": canaries[-1], "token": token,
               "word": "cumulative_cut", "amount": f"{START - LOSS:,.0f}"}[kind]
    assert _leaks(_all_text(rig, caplog, journal), view, token, canaries) == []
    journal.logger.getChild("plant").error("x %s", planted)
    assert _leaks(_all_text(rig, caplog, journal), view, token, canaries)

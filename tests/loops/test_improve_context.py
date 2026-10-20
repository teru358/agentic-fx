"""build_improve_context (設計書 §3.2)。各節が §3.2 表の出所どおりに
集計されることを節ごとに検証する。"""
from __future__ import annotations

import json

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.config import load_settings
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.store import backlog
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
# precheck 2026-08-22: T8-B3 -- `settings` fixture does not exist anywhere in
# the repo (tests/conftest.py has no `def settings`, and tests/loops/ has no
# conftest.py). Follow the existing convention in
# tests/loops/test_improve_forbidden.py:29 -- a module-level constant loaded
# from the example settings file -- instead of a nonexistent fixture.
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_context_has_all_top_level_sections(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert set(ctx.keys()) >= {
        "performance_report", "improvement_history", "current_inventory",
        "backlog", "user_policy", "references"}


def test_backlog_section_marks_partition_when_hint_given(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    b1 = backlog.add(c, "idea1", "user", NOW)
    b2 = backlog.add(c, "idea2", "user", NOW)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=frozenset({b1}))
    items = {i["id"]: i for i in ctx["backlog"]["items"]}
    assert items[b1]["assigned"] is True
    assert items[b2]["assigned"] is False


def test_backlog_section_no_partition_mark_for_manual_wave(tmp_path):
    """手動 wave (allowed_backlog_ids=None) は印を付けない (§3.2 表)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    b1 = backlog.add(c, "idea1", "user", NOW)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    item = next(i for i in ctx["backlog"]["items"] if i["id"] == b1)
    assert "assigned" not in item


def test_backlog_notes_are_newest_twenty_with_omission_count(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    ids = []
    for i in range(25):
        bid = backlog.add(c, f"note-{i}", "agent", NOW)
        backlog.set_status(c, bid, "note", NOW, last_result="human_noted")
        ids.append(bid)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert [n["id"] for n in ctx["backlog"]["notes"]] == list(reversed(ids))[:20]
    assert ctx["backlog"]["notes_omitted"] == 5


def test_backlog_section_includes_attempts_and_trial_count(tmp_path):
    """§3.2「各バックログ課題の試行回数と、strategy なら標本 (取引数) を
    添える (R8)」。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    backlog.set_status(c, bid, "observation", NOW, last_result="insufficient_trades:5")
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    item = next(i for i in ctx["backlog"]["items"] if i["id"] == bid)
    assert item["attempts"] == 1
    assert item["last_result"] == "observation:not_submitted"


def test_user_policy_section_is_tail_4000_chars(tmp_path):
    policy_path = tmp_path / "policy" / "directives.md"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("x" * 5000)
    c = connect(tmp_path / "t.db"); init_db(c)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert len(ctx["user_policy"]["tail"]) == 4000


def test_references_section_has_staging_and_naming_convention(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert "plugin_name_pattern" in ctx["references"]
    assert ctx["references"]["plugin_name_pattern"] == "^[a-z][a-z0-9_]{0,63}$"


def test_performance_report_has_win_rate_and_pf_keys(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert {"win_rate", "profit_factor", "by_pair", "by_hour", "reject_breakdown",
           "hold_rate"} <= set(ctx["performance_report"].keys())


def test_performance_report_window_is_90_days_not_30(tmp_path):
    """M16 (段 0 Minor): 集計窓 `timedelta(days=90)` を `days=30` にする
    変異が red になる pin — §3.2 の `window_days: [30, 90]` 表示は
    30/90 の両方を対応窓として謳うが、実集計窓は 90 日。60 日前 (30 日
    窓なら圏外、90 日窓なら圏内) の closed order を仕込み、by_pair の
    件数がそれを拾うことを見る。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    old = NOW - timedelta(days=60)   # 30 日窓なら圏外、90 日窓なら圏内
    recent = NOW - timedelta(days=10)
    for created_at in (old, recent):
        c.execute(
            "INSERT INTO orders (pair,direction,entry_type,horizon,status,"
            "realized_pnl,created_at,updated_at) VALUES "
            "('USDJPY','long','market','day','closed',1.0,?,?)",
            (created_at.isoformat(), created_at.isoformat()))
    c.commit()
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert ctx["performance_report"]["by_pair"]["USDJPY"]["count"] == 2


def test_performance_report_window_boundary_narrows_to_90_days_not_61(tmp_path):
    """A23 是正 (束D検収, verified-local-round1.md §11 #16):
    上のテストは `-60d`/`-10d` の 2 点しか実測しておらず、窓が
    `(60, 90]` の範囲では無防備だった (`days=90→61` の変異は両方が窓内
    のまま SURVIVED、`days=45`/`30` は `-60d` が外れて KILLED — 詳細は
    verified-local-round1.md §1)。`-85d` (90 日窓なら圏内、61 日窓なら
    圏外) の closed order を 1 本足し、`days=61` への変異も red にする。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    old = NOW - timedelta(days=85)   # 61 日窓なら圏外、90 日窓なら圏内
    recent = NOW - timedelta(days=10)
    for created_at in (old, recent):
        c.execute(
            "INSERT INTO orders (pair,direction,entry_type,horizon,status,"
            "realized_pnl,created_at,updated_at) VALUES "
            "('USDJPY','long','market','day','closed',1.0,?,?)",
            (created_at.isoformat(), created_at.isoformat()))
    c.commit()
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    assert ctx["performance_report"]["by_pair"]["USDJPY"]["count"] == 2


def test_inventory_view_overrides_the_live_plugins_dir(tmp_path):
    """[indicator-consumption-wiring] P3 / 段 0 束 3 M6: `inventory_view` が
    渡されたら prompt の `approved_plugins` は **その view だけ**から作る —
    live `plugins/` は読まない (prepare 後に配備が変わっても mission 中の
    prompt は不変)。

    `if inventory_view is not None:` を `if False:` にして live 経路へ
    倒す変異は判定 suite 全体 (1355 passed) が green のままだった (実測) —
    既存テストはどれも view と live `plugins/` が同じ答えを返す形
    (fixture の縮退) なので、どちらの経路を通ったか区別できていなかった。
    ここでは **live に何も無い / view にだけ居る** 形にして経路を分ける。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "a" * 64}],
            "pin_broken_strategies": []}
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None, inventory_view=view)
    assert [p["name"] for p in ctx["current_inventory"]["approved_plugins"]] \
        == ["rsi"]
    # 遮断 8: view にしか無い情報でも成績・期間・段名は載らない
    assert set(ctx["current_inventory"]["approved_plugins"][0]) == {
        "name", "kind", "pairs", "params", "outputs", "content_hash"}


def test_context_never_carries_the_human_logs(tmp_path):
    """次回 mission の材料は DB と方針ファイルから組み、人間向けの技術ログ・
    activity の内容も位置も載せない。"""
    import json

    from agentic_fx.activity import ActivityLog, Category

    marker = "HUMAN-LOG-MARKER-7b3e"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agentic.log").write_text(
        f"plugin_worker_diagnostic code=cpu_limit stderr_tail={marker}\n")
    ActivityLog(logs / "activity.log").write(
        Category.IMPROVE, "backtest_cpu",
        f"mission=1 plugin={marker} cpu_sec=59.97 cpu_source=parent_wait4 "
        "result=cpu_limit returncode=-9 signal=9")
    c = connect(tmp_path / "t.db"); init_db(c)
    backlog.add(c, "idea1", "user", NOW)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    text = json.dumps(ctx, default=str, ensure_ascii=False)
    for needle in (marker, "agentic.log", "activity.log", "59.97",
                   "parent_wait4", "stderr_tail"):
        assert needle not in text, needle


_BACKLOG_CANARIES = (
    "insufficient_trades:7777", "gate_failed:holdout_pf=0.7391 trades=4242",
    "observation:evaluable_false_canary", "report_failed:pf 1.2345 leaked",
    "mission_failed:holdout 0.8812", "weird free text 5551")


def test_backlog_last_result_is_projected_in_items_and_notes(tmp_path):
    from agentic_fx.loops.improve_context import project_last_result
    c = connect(tmp_path / "t.db"); init_db(c)
    ids = []
    for i, value in enumerate(_BACKLOG_CANARIES):
        bid = backlog.add(c, f"canary idea {i}", "user", NOW)
        status = "note" if i % 2 else "observation"
        backlog.set_status(c, bid, status, NOW, last_result=value)
        ids.append(bid)
    stored = [r["last_result"] for r in c.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id")]
    assert stored == list(_BACKLOG_CANARIES)
    ctx = build_improve_context(c, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    rows = ctx["backlog"]["items"] + ctx["backlog"]["notes"]
    assert rows
    blob = json.dumps(ctx["backlog"])
    for token in ("7777", "0.7391", "4242", "evaluable_false_canary",
                  "1.2345", "0.8812", "5551", "free text"):
        assert token not in blob
    for row in rows:
        assert row["last_result"] == project_last_result(
            _BACKLOG_CANARIES[ids.index(row["id"])])


@pytest.mark.parametrize("value,expected", [
    ("gate_failed:holdout_pf=0.7391", "gate_failed"),
    ("report_failed:pf 1.2345", "report_failed"),
    ("observation:something 12", "observation"),
    ("totally unknown 99", "unclassified"),
    ("human_noted best=x@abc pf=1.31 dd=0.2 archive=/p", "human_noted"),
    ("gate_failed:noop_copy_of:sma_cross", "gate_failed:noop_copy_of:sma_cross"),
    ("approval_pending:12", "approval_pending:12"),
    ("rejected_by_human", "rejected_by_human"),
    ("mission #3 status=failed reason=tool_budget_abort:x run_backtest=2 "
     "analyze_corr=0 best=n@abcd1234 pf=1.31 dd=0.2 archive=/p",
     "mission_diagnostic run_backtest=2 analyze_corr=0"),
])
def test_project_last_result_is_fail_closed(value, expected):
    from agentic_fx.loops.improve_context import project_last_result
    assert project_last_result(value) == expected

"""build_improve_context (設計書 §3.2)。各節が §3.2 表の出所どおりに
集計されることを節ごとに検証する。"""
from __future__ import annotations

from datetime import datetime, timezone
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
    assert item["last_result"] == "insufficient_trades:5"


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

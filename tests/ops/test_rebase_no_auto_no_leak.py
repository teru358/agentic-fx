"""HWM の付け直しは人の明示操作だけが動かし、金額は専用の preview と DB の台帳にしか出ない。

reset・reconcile・自動の再ラッチ・起動時の回復は HWM と台帳を変えない。通常の status・
event・journal・audit に新しい金額 field も preview の中身も出ない。"""
from __future__ import annotations

import json
import logging

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


def _all_text(rig, caplog):
    parts = [rig.env.activity_path.read_text(encoding="utf-8") if rig.env.activity_path.exists()
             else ""]
    for table, columns in (("ops_requests", "*"), ("ops_events", "*")):
        for row in rig.conn.execute(f"SELECT {columns} FROM {table}").fetchall():
            parts.append(json.dumps([str(value) for value in tuple(row)], ensure_ascii=False))
    parts.append(caplog.text)
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

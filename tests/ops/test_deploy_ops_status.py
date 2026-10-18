"""status が ``plugin_deploy_ops`` から未完・要注意の配備操作を返す。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_fx.ops import client
from agentic_fx.store import plugin_deploy_ops as deploy_store

from .conftest import OPERATOR


def _intent(conn, name, *, at):
    return deploy_store.insert_intent(
        conn, op_kind="disable_symlink", name=name, from_kind="symlink", to_kind="absent",
        from_target=f".versions/{name}/{'a' * 64}", to_target=None, retired_path=None,
        subject_content_hash="c" * 64, subject_artifact_hash="a" * 64,
        target_content_hash=None, target_artifact_hash=None, approval_id=None,
        revoke=False, revoke_row_id=None, force=False, actor="human_cli", now=at)


def _rollback_intent(conn, name, *, at):
    return deploy_store.insert_intent(
        conn, op_kind="rollback_symlink", name=name, from_kind="symlink", to_kind="symlink",
        from_target=f".versions/{name}/{'a' * 64}", to_target=f".versions/{name}/{'b' * 64}",
        retired_path=None, subject_content_hash="c" * 64, subject_artifact_hash="a" * 64,
        target_content_hash="d" * 64, target_artifact_hash="b" * 64, approval_id=None,
        revoke=False, revoke_row_id=None, force=False, actor="human_cli", now=at)


def _phase(conn, op_id, phase, *, at, fail_reason=None, notified=None):
    deploy_store.set_phase(conn, op_id, phase, now=at, fail_reason=fail_reason,
                           notify_attempted=notified)


def test_status_lists_open_ops_and_ops_needing_attention(ops_env):
    conn = ops_env.conn
    now = ops_env.wall()
    stuck = _intent(conn, "sma", at=now)
    _phase(conn, stuck, "needs_retry", fail_reason="syscall_failed", at=now)
    hour_ago = now - timedelta(hours=1)
    unverified = _rollback_intent(conn, "ema", at=hour_ago)
    _phase(conn, unverified, "needs_retry", fail_reason="verify_failed", notified=True,
           at=hour_ago)
    days_ago = now - timedelta(days=3)
    foreign = _intent(conn, "rsi", at=days_ago)
    _phase(conn, foreign, "foreign", notified=False, at=days_ago)
    old_foreign = _intent(conn, "old", at=days_ago)
    _phase(conn, old_foreign, "foreign", notified=True, at=days_ago)
    done = _intent(conn, "fine", at=now)
    _phase(conn, done, "done", notified=True, at=now)

    section = ops_env.service().status(OPERATOR)["plugin_deploy_ops"]

    assert [row["name"] for row in section["open"]] == ["sma"]
    assert section["open"][0]["phase"] == "needs_retry"
    assert section["open"][0]["fail_reason"] == "syscall_failed"
    assert section["open"][0]["op_id"] == stuck
    assert sorted(row["name"] for row in section["attention"]) == ["ema", "rsi"]


def test_status_without_deploy_ops_has_empty_sections(ops_env):
    section = ops_env.service().status(OPERATOR)["plugin_deploy_ops"]

    assert section == {"open": [], "attention": []}


def test_the_notification_window_keeps_an_old_foreign_op_listed_until_it_is_sent(tmp_path):
    from agentic_fx.store import db as db_store

    conn = db_store.connect(tmp_path / "window.db")
    db_store.init_db(conn)
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    long_ago = now - timedelta(days=9)
    op_id = _intent(conn, "x", at=long_ago)
    _phase(conn, op_id, "foreign", notified=False, at=long_ago)
    assert [r["name"] for r in deploy_store.status_summary(conn, now=now)["attention"]] == ["x"]
    deploy_store.mark_notify_attempted(conn, op_id)
    assert deploy_store.status_summary(conn, now=now)["attention"] == []


def test_ctl_status_shows_open_ops_and_stays_silent_without_them():
    quiet = client.format_status({"plugin_deploy_ops": {"open": [], "attention": []}})
    assert "配備操作" not in quiet

    text = client.format_status({"plugin_deploy_ops": {
        "open": [{"op_id": 5, "name": "sma", "op_kind": "disable_symlink",
                  "phase": "needs_retry", "fail_reason": "syscall_failed"}],
        "attention": [{"op_id": 6, "name": "ema", "op_kind": "rollback_symlink",
                       "phase": "needs_retry", "fail_reason": "verify_failed"}]}})

    assert ("配備操作が未完: 1 件 (sma disable_symlink needs_retry/syscall_failed op_id=5)"
            " — `afx plugin versions <name>`") in text
    assert "配備操作に要注意: 1 件 (ema rollback_symlink needs_retry/verify_failed op_id=6)" in text


def test_ctl_status_sanitises_control_characters_in_a_plugin_name():
    text = client.format_status({"plugin_deploy_ops": {"open": [
        {"op_id": 1, "name": "bad\x1b[31mname", "op_kind": "disable_symlink",
         "phase": "intent", "fail_reason": None}], "attention": []}})

    assert "\x1b" not in text

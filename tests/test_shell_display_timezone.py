"""対話シェルの時刻は設定の display_timezone で出る。保存は UTC のまま。"""
from __future__ import annotations

from datetime import timedelta

from agentic_fx.activity import Category

from tests.test_commands import (
    NOW, SETTINGS, _commands, _commands_with_outage, _pending_plugin_approval, _stall)

assert SETTINGS.display_timezone == "Asia/Tokyo"


def _tokyo(cmds):
    cmds.settings = SETTINGS
    return cmds


def test_status_starts_with_the_zone_and_offset(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert _tokyo(cmds).dispatch("status").splitlines()[0] == "表示 tz: Asia/Tokyo (+09:00)"


def test_status_without_settings_names_utc(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert cmds.dispatch("status").splitlines()[0] == "表示 tz: UTC (+00:00)"


def test_status_converts_the_latch_time_and_the_data_gap_times(tmp_path):
    conn, state, _, cmds, outage = _commands_with_outage(tmp_path)
    _tokyo(cmds)
    assert _stall(conn, outage, NOW)[0] == "restricted"
    state.update(kill_switch_latched=True)
    state.update(kill_switch_latched_at="2026-07-22T03:04:05+00:00")
    out = cmds.dispatch("status")
    assert "since=2026-07-22 12:04:05 +09:00" in out
    assert "since 2026-07-22 20:52:30 +09:00, deadline 2026-07-22 21:22:30 +09:00" in out
    assert "gap: USDJPY/1m since 2026-07-22 20:50:00 +09:00" in out


def test_restricted_activity_body_carries_utc_tokens_that_the_display_converts(tmp_path):
    conn, _, activity, cmds, outage = _commands_with_outage(tmp_path)
    _tokyo(cmds)
    _stall(conn, outage, NOW)
    stored = [line for line in activity.tail(20) if "datafeed_restricted" in line]
    assert len(stored) == 1
    assert ("expected=2026-07-22T11:52:30Z deadline=2026-07-22T12:22:30Z") in stored[0]
    shown = [line for line in cmds.dispatch("activity 20").splitlines()
             if "datafeed_restricted" in line]
    assert len(shown) == 1
    assert "expected=2026-07-22 20:52:30 +09:00 deadline=2026-07-22 21:22:30 +09:00" in shown[0]


def test_activity_head_is_converted_and_the_file_is_not_changed(tmp_path):
    _, _, activity, cmds = _commands(tmp_path)
    path = tmp_path / "logs" / "activity.log"
    path.write_text(
        "2026-11-06T10:04:05+00:00\tAGGREGATE\tdecision\t2026-11-06T10:00:00Z の判断\t-\n"
        "2026-11-06T10:05:00+00:00\tNEWS\tnews_source_skipped\t"
        "src: 連続 3 回失敗のため 2026-11-06T11:00:00Z まで待つ\t-\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    out = _tokyo(cmds).dispatch("activity 5").splitlines()
    assert out[0] == "2026-11-06 19:04:05 +09:00\tAGGREGATE\tdecision\t2026-11-06T10:00:00Z の判断\t-"
    assert out[1] == ("2026-11-06 19:05:00 +09:00\tNEWS\tnews_source_skipped\t"
                      "src: 連続 3 回失敗のため 2026-11-06 20:00:00 +09:00 まで待つ\t-")
    assert path.read_text(encoding="utf-8") == before


def test_log_head_is_converted(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    (tmp_path / "logs" / "agentic.log").write_text(
        "2026-11-06 10:14:23,090 UTC INFO agentic_fx.x: hello\ncontinued line\n",
        encoding="utf-8")
    assert _tokyo(cmds).dispatch("log 5").splitlines() == [
        "2026-11-06 19:14:23,090 +09:00 INFO agentic_fx.x: hello", "continued line"]


def test_approval_list_and_detail_times_are_converted(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    _tokyo(cmds)
    approval_id = _pending_plugin_approval(conn)
    assert (f"#{approval_id} kind=plugin name=sma created_at=2026-07-22 21:00:00 +09:00 "
            in cmds.dispatch("approval list"))
    conn.execute("UPDATE approval_requests SET decided_at=? WHERE id=?",
                 ((NOW + timedelta(minutes=5)).isoformat(), approval_id))
    conn.commit()
    assert "decided_at=2026-07-22 21:05:00 +09:00" in cmds.dispatch(f"approval {approval_id}")


def test_the_kill_switch_reset_activity_body_is_a_utc_token_in_every_zone(tmp_path):
    _, state, activity, cmds = _commands(tmp_path)
    _tokyo(cmds)
    state.update(kill_switch_latched=True)
    state.update(kill_switch_latched_at="2026-07-22T03:04:05+00:00")
    cmds.dispatch(f"killswitch reset {state.load().kill_switch_generation}")
    stored = [line for line in activity.tail(10, Category.SYSTEM) if "kill_switch_reset" in line]
    assert "latched_at=2026-07-22T03:04:05Z" in stored[0]
    shown = [line for line in cmds.dispatch("activity 10 system").splitlines()
             if "kill_switch_reset" in line]
    assert "latched_at=2026-07-22 12:04:05 +09:00" in shown[0]


def test_status_warns_about_a_non_region_zone_name(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    cmds.settings = SETTINGS.model_copy(update={"display_timezone": "EST"})
    out = cmds.dispatch("status")
    assert "表示 tz: EST (-05:00)" in out
    assert "warning: display_timezone 'EST' は地域名ではありません" in out
    assert "warning" not in _tokyo(cmds).dispatch("status")

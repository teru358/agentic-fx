"""status の econ_signal は許可投影を通り、ctl の表示に calendar と容量が出る。"""
from __future__ import annotations

import pytest

from agentic_fx.loops import econ_signal_report as esr
from agentic_fx.ops import client
from agentic_fx.ops.contracts import ShellCaller

REASONS = esr.ABANDON_REASONS


def _econ(**over):
    econ = {
        "slice_worker": "alive",
        "decisions": {"total": 5, "by_kind": {"accept": 3, "reject:system:econ_window": 2}},
        "abandoned": {"total": {r: 0 for r in REASONS} | {"retry_exhausted": 4},
                      "latest_day": {r: 0 for r in REASONS} | {"freshness_expired": 1},
                      "latest_day_start": "2026-11-05T22:00:00+00:00",
                      "cleanup": {"total": 2, "latest_day": 1}, "legacy_unknown_total": 9},
        "missions_latest_day": {"system_filtered": 2, "failed": 1, "running": 0,
                                "completed": 4, "skipped": 0, "other": 0},
        "shadow": [{"decision": "accept", "actor": "system", "reject_code": None,
                    "state": "resolved", "result_code": "tp", "n": 3,
                    "avg_pnl_pips": 1.5}],
        "slice_capacity": {
            "signal_daily": 96, "signal_producer": 96, "signal_override": 120,
            "capacity_override_below_producer": True, "rows": 80, "row_capacity": 100,
            "row_level": "warn", "slice_bytes": 7000,
            "slice_bytes_measured_at": "2026-11-06T01:02:03+00:00",
            "used_bytes": 400_000, "byte_capacity": 10_000_000, "byte_level": "ok",
            "base_bytes": 348_160, "bytes_per_row": 177, "level": "warn",
            "db_bytes": 11, "wal_bytes": 12, "free_bytes": 13}}
    econ.update(over)
    return econ


def _status(env, provided):
    service = env.service(status_provider=lambda: provided)
    return service.status(ShellCaller.SHELL)


def test_status_passes_allowlisted_econ_fields_through(ops_env):
    out = _status(ops_env, {"econ_signal": _econ()})["econ_signal"]
    assert out["slice_worker"] == "alive"
    assert out["decisions"]["by_kind"]["reject:system:econ_window"] == 2
    assert out["abandoned"]["total"]["retry_exhausted"] == 4
    assert out["abandoned"]["legacy_unknown_total"] == 9
    assert out["missions_latest_day"]["system_filtered"] == 2
    assert out["slice_capacity"]["capacity_override_below_producer"] is True
    assert out["slice_capacity"]["signal_override"] == 120
    assert out["slice_capacity"]["slice_bytes_measured_at"] == "2026-11-06T01:02:03+00:00"


def test_status_drops_fields_and_values_outside_the_allowlist(ops_env):
    hostile = _econ()
    hostile["secret"] = "x"
    hostile["slice_worker"] = "\x1b[31mboom"
    hostile["slice_capacity"]["note"] = "leak"
    hostile["slice_capacity"]["rows"] = "80"
    hostile["slice_capacity"]["level"] = "\x1b[31mred"
    hostile["decisions"]["by_kind"]["bad key\n"] = 1
    hostile["shadow"][0]["actor"] = "a\nb"
    hostile["shadow"][0]["extra"] = 1
    out = _status(ops_env, {"econ_signal": hostile})["econ_signal"]
    assert "secret" not in out
    assert out["slice_worker"] == "unknown"
    assert "note" not in out["slice_capacity"]
    assert out["slice_capacity"]["rows"] is None and out["slice_capacity"]["level"] is None
    assert "bad key\n" not in out["decisions"]["by_kind"]
    assert out["shadow"][0]["actor"] is None and "extra" not in out["shadow"][0]


def test_a_malformed_econ_section_is_left_out(ops_env):
    assert "econ_signal" not in _status(ops_env, {"econ_signal": "junk"})


def test_status_without_econ_section_is_unchanged(ops_env):
    assert "econ_signal" not in _status(ops_env, {})


# --- ctl の表示 -------------------------------------------------------------------

def test_ctl_status_shows_capacity_abandoned_decisions_and_worker(ops_env):
    text = client.format_status({"econ_signal": _status(
        ops_env, {"econ_signal": _econ()})["econ_signal"]})
    assert "econ signal: worker=alive" in text
    assert "decisions: 5 accept=3, reject:system:econ_window=2" in text
    assert "abandoned (latest day/total): freshness_expired=1/0, retry_exhausted=0/4" in text
    assert "cleanup=1/2 legacy_unknown=9" in text
    assert "system_filtered=2 failed=1 running=0" in text
    assert "shadow: accept/system/-:resolved/tp=3" in text
    assert ("slice capacity: rows 80/100 (warn) bytes 400000/10000000 (ok) S=96 "
            "(producer=96 override=120) B0=348160 beta=177 db=11 wal=12 free=13") in text
    assert "slice bytes: 7000 (measured_at=2026-11-06T01:02:03+00:00)" in text
    assert "warning: capacity_override_below_producer" in text
    assert "warning: slice capacity warn" in text


def test_ctl_status_says_unmeasured_when_the_worker_has_not_measured(ops_env):
    capacity = dict(_econ()["slice_capacity"], slice_bytes=None, used_bytes=None,
                    byte_level=None, slice_bytes_measured_at=None)
    text = client.format_status({"econ_signal": _status(
        ops_env, {"econ_signal": _econ(slice_capacity=capacity)})["econ_signal"]})
    assert "bytes 未測定" in text
    assert "slice bytes:" not in text


def test_ctl_status_survives_a_broken_econ_section():
    assert "econ signal" not in client.format_status({"econ_signal": "junk"})
    text = client.format_status({"econ_signal": {"slice_worker": "alive",
                                                 "decisions": {"total": 1}}})
    assert "econ signal: 表示できません" in text


def test_ctl_status_shows_system_filtered_as_the_missions_terminal():
    text = client.format_status({"mission": {"loop": "trade",
                                             "status": "system_filtered",
                                             "started_at": "t"}})
    assert "system_filtered" in text


@pytest.mark.parametrize("freshness,word", [
    ("unknown", "unknown"), ("fresh", "fresh"), ("stale", "stale"),
    ("\x1b[31mjunk", "unknown"), (None, "unknown")])
def test_ctl_status_shows_the_calendar_state(freshness, word):
    text = client.format_status({
        "worst_case_guarantee_unavailable": False,
        "window": {"in_window": False, "active_events": 0, "freshness": freshness}})
    assert f"calendar={word}" in text
    assert "\x1b" not in text


def test_calendar_states_follow_the_attempt_record(tmp_path):
    """初回成功なし / 成功直後 / 直近 failure / 12h 超 / 回復 を実際の鮮度判定で通す。"""
    from datetime import datetime, timedelta, timezone

    from agentic_fx.store import fetch_attempts
    from agentic_fx.store.db import connect, init_db

    conn = connect(tmp_path / "c.db")
    init_db(conn)
    t0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    key = fetch_attempts.ECON_KEY

    def word():
        return client._calendar_word({"freshness": fetch_attempts.freshness(
            conn, key, now)})

    now = t0
    assert word() == "unknown"
    fetch_attempts.record_outcome(conn, key, now, ok=False)
    assert word() == "unknown"
    fetch_attempts.record_outcome(conn, key, t0, ok=True)
    now = t0 + timedelta(hours=1)
    assert word() == "fresh"
    fetch_attempts.record_outcome(conn, key, now, ok=False)
    assert word() == "stale"
    fetch_attempts.record_outcome(conn, key, now, ok=True)
    assert word() == "fresh"
    now = t0 + timedelta(hours=1 + 12, minutes=1)
    assert word() == "stale"
    fetch_attempts.record_outcome(conn, key, now, ok=True)
    assert word() == "fresh"
    conn.close()



def test_web_copy_of_the_abandon_reasons_matches_the_daemon():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops" / "web"
              / "render.py").read_text(encoding="utf-8")
    copied = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                  if isinstance(node, ast.Assign)
                  and node.targets[0].id == "ECON_ABANDON_REASONS")
    assert tuple(copied) == esr.ABANDON_REASONS

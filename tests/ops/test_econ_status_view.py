"""status の econ_signal は許可投影を通り、ctl の表示に calendar と容量が出る。"""
from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.parametrize("bad", [[], {"a": 1}, 7, None])
def test_unhashable_or_wrongly_typed_values_never_break_status(ops_env, bad):
    hostile = _econ()
    hostile["slice_worker"] = bad
    hostile["slice_capacity"]["level"] = bad
    hostile["slice_capacity"]["row_level"] = bad
    hostile["shadow"][0]["actor"] = bad
    out = _status(ops_env, {"econ_signal": hostile})["econ_signal"]
    assert out["slice_worker"] == "unknown"
    assert out["slice_capacity"]["level"] is None
    assert out["shadow"][0]["actor"] is None
    assert out["decisions"]["total"] == 5


def test_status_survives_a_list_where_a_section_is_expected(ops_env):
    out = _status(ops_env, {"econ_signal": {"slice_worker": []}})["econ_signal"]
    assert out["slice_worker"] == "unknown"
    assert out["decisions"] is None and out["slice_capacity"] is None


def test_one_broken_section_does_not_drop_the_others(ops_env, monkeypatch):
    from agentic_fx.ops import service as svc

    def boom(raw):
        raise RuntimeError("x")

    monkeypatch.setattr(svc, "_econ_abandoned", boom)
    out = _status(ops_env, {"econ_signal": _econ()})["econ_signal"]
    assert out["abandoned"] is None
    assert out["decisions"]["total"] == 5


def test_values_outside_the_known_sets_or_ranges_are_dropped(ops_env):
    hostile = _econ()
    hostile["shadow"][0].update(actor="banana", decision="unknown", state="weird",
                                result_code="zzz", reject_code="nope", n=-1)
    hostile["decisions"]["by_kind"]["reject:banana:econ_window"] = 1
    hostile["decisions"]["by_kind"]["accept"] = -3
    hostile["slice_capacity"]["rows"] = -1
    hostile["slice_capacity"]["slice_bytes_measured_at"] = "not-a-time"
    hostile["abandoned"]["latest_day_start"] = "xxxxxxxxxxxx"
    out = _status(ops_env, {"econ_signal": hostile})["econ_signal"]
    row = out["shadow"][0]
    assert (row["actor"], row["decision"], row["state"], row["result_code"],
            row["reject_code"], row["n"]) == (None,) * 6
    assert out["decisions"]["by_kind"] == {"reject:system:econ_window": 2, "other": 1}
    assert out["slice_capacity"]["rows"] is None
    assert out["slice_capacity"]["slice_bytes_measured_at"] is None
    assert out["abandoned"]["latest_day_start"] is None


def test_known_shadow_words_match_the_stored_vocabulary(ops_env):
    row = {"decision": "reject", "actor": "llm", "reject_code": "news_shock",
           "state": "resolved", "result_code": "data_insufficient", "n": 1,
           "avg_pnl_pips": None}
    out = _status(ops_env, {"econ_signal": _econ(shadow=[row])})["econ_signal"]
    assert {k: out["shadow"][0][k] for k in row if k not in ("n", "avg_pnl_pips")} == {
        k: row[k] for k in row if k not in ("n", "avg_pnl_pips")}


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
                                                 "decisions": {"total": 1,
                                                               "by_kind": 5}}})
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


# --- 未知の語・省略・測定の状態 ---------------------------------------------------------

def test_unknown_decision_kinds_are_counted_as_other_and_the_sum_matches_the_total(ops_env):
    econ = _econ(decisions={"total": 10, "by_kind": {
        "accept": 3, "reject:llm:brand_new_code": 4, "mystery": 3}})
    out = _status(ops_env, {"econ_signal": econ})["econ_signal"]["decisions"]
    assert out["by_kind"] == {"accept": 3, "other": 7}
    assert sum(out["by_kind"].values()) == out["total"]
    text = client.format_status({"econ_signal": _status(
        ops_env, {"econ_signal": econ})["econ_signal"]})
    assert "その他=7" in text


def test_shadow_groups_beyond_the_cap_are_counted_as_omitted(ops_env):
    row = _econ()["shadow"][0]
    out = _status(ops_env, {"econ_signal": _econ(shadow=[dict(row) for _ in range(70)])}
                  )["econ_signal"]
    assert len(out["shadow"]) == 64 and out["shadow_omitted"] == 6
    few = _status(ops_env, {"econ_signal": _econ()})["econ_signal"]
    assert few["shadow_omitted"] == 0
    text = client.format_status({"econ_signal": out})
    assert "shadow: 省略 6 組" in text


def test_measurement_state_passes_the_projection_and_unknown_words_do_not(ops_env):
    capacity = dict(_econ()["slice_capacity"], measure_outcome="aborted",
                    measure_elapsed_sec=30.04, slice_bytes_stale=True)
    out = _status(ops_env, {"econ_signal": _econ(slice_capacity=capacity)}
                  )["econ_signal"]["slice_capacity"]
    assert out["measure_outcome"] == "aborted"
    assert out["measure_elapsed_sec"] == 30.0
    assert out["slice_bytes_stale"] is True
    bad = dict(capacity, measure_outcome="exploded", measure_elapsed_sec=-1,
               slice_bytes_stale="yes")
    out = _status(ops_env, {"econ_signal": _econ(slice_capacity=bad)}
                  )["econ_signal"]["slice_capacity"]
    assert (out["measure_outcome"], out["measure_elapsed_sec"],
            out["slice_bytes_stale"]) == (None, None, None)


# --- web の写しとの突き合わせ ----------------------------------------------------------

def _web_constants():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops" / "web"
              / "render.py").read_text(encoding="utf-8")
    return {node.targets[0].id: ast.literal_eval(node.value)
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.isupper() | node.targets[0].id.startswith("_")
            and isinstance(node.value, (ast.Tuple, ast.Constant))}


def test_web_copies_of_the_daemon_vocabulary_match():
    from agentic_fx.core import econ_window
    from agentic_fx.ops import service as ops_service

    web = _web_constants()
    assert tuple(web["ECON_ABANDON_REASONS"]) == esr.ABANDON_REASONS
    assert tuple(web["_CALENDAR_STATES"]) == econ_window.FRESHNESS_VALUES
    assert set(web["_WORKER_STATES"]) == set(ops_service._ECON_WORKER_STATES)
    assert set(web["_WORKER_STATES"]) - {"unknown"} == {
        "alive", "stopped", "start_failed", "not_started"}
    assert set(web["_LEVELS"]) == set(ops_service._ECON_LEVELS)
    assert set(web["_MEASURE_OUTCOMES"]) == set(ops_service._ECON_MEASURE_OUTCOMES)
    assert web["_OTHER_KIND"] == esr.OTHER_KIND
    assert set(web["_MISSION_KEYS"]) <= {*esr.KNOWN_MISSION_TERMINALS, "running"}


def test_web_capacity_keys_are_exactly_what_the_daemon_reports(tmp_path):
    web = _web_constants()
    plan = SimpleNamespace(rows=10, bytes=10**6, signals=SimpleNamespace(
        value=1, producer=1, override=None, below_producer=False))
    reported = set(esr.capacity_status(plan, rows=1, slice_bytes=1, db_path=tmp_path / "d.db",
                                       free_bytes=1, base_bytes=0))
    assert reported == set(web["_CAPACITY_KEYS"]) | {"capacity_override_below_producer"}


def test_the_client_has_no_second_copy_of_the_calendar_words():
    import ast
    from pathlib import Path

    from agentic_fx.core import econ_window

    source = (Path(client.__file__)).read_text(encoding="utf-8")
    words = set(econ_window.FRESHNESS_VALUES)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            literal = {e.value for e in node.elts if isinstance(e, ast.Constant)}
            assert not words <= literal, f"line {node.lineno}"
    for word in econ_window.FRESHNESS_VALUES:
        assert client._calendar_word({"freshness": word}) == word
    assert client._calendar_word({"freshness": "bogus"}) == "unknown"

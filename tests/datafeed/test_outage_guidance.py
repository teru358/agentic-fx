"""status の案内 (phase / next_action) の真理値表と、API に載せる読み取り。

期待値は設計の表から手で書いた値。表は上の行ほど優先する。
"""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.datafeed import guidance_text
from agentic_fx.datafeed.outage import (GuidanceFacts, IngestTickReport, OutageStateMachine,
                                        resume_guidance, unknown_guidance)
from agentic_fx.core.contracts import Bar
from agentic_fx.store import orders, outage_facts
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
KEY = (PAIR, "1m")
UTC = timezone.utc
WED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
WIDTHS = {"1m": timedelta(minutes=1)}


def _g(*, failed=False, state="degraded", request=False, result=None, mark=False, count=0):
    return resume_guidance(observe_failed=failed, state=state, request_pending=request,
                           last_result=result, awaiting_mark=mark, unprocessed=count)


# ---- 真理値表の全行 (入口 x 状態) ----------------------------------------

TABLE = [
    # (failed, state, request, result, mark, count) -> (phase, next_action)
    (dict(failed=True, state="degraded"), ("unknown", "check_status")),
    (dict(failed=True, state="ready"), ("unknown", "check_status")),
    (dict(failed=True, state="degraded", request=True), ("unknown", "check_status")),
    (dict(state="degraded", request=True), ("resume_requested", "wait_tick")),
    (dict(state="restricted", request=True), ("resume_requested", "wait_tick")),
    (dict(state="ready", request=True), ("resume_requested", "wait_tick")),
    (dict(state="degraded", request=True, result="rejected_watermark"),
     ("resume_requested", "wait_tick")),
    (dict(state="ready"), ("ready", "none")),
    (dict(state="ready", result="rejected_watermark", mark=True), ("ready", "none")),
    (dict(state="degraded", result="rejected_watermark"), ("resume_rejected", "restore_source")),
    (dict(state="restricted", result="rejected_watermark"), ("resume_rejected", "restore_source")),
    (dict(state="degraded", result="rejected_watermark", count=3),
     ("resume_rejected", "restore_source")),
    (dict(state="degraded", result="rejected_unprocessed", count=2),
     ("resume_rejected", "resume_with_ack")),
    (dict(state="restricted", result="rejected_unprocessed", count=2),
     ("resume_rejected", "resume_with_ack")),
    (dict(state="degraded", result="rejected_unprocessed", count=0), ("resume_rejected", "resume")),
    (dict(state="restricted", result="rejected_unprocessed", count=0),
     ("resume_rejected", "resume")),
    (dict(state="degraded", mark=True, count=2), ("awaiting_resume", "resume_with_ack")),
    (dict(state="degraded", mark=True, count=0), ("awaiting_resume", "resume")),
    (dict(state="degraded", mark=False, count=2), ("stopped", "restore_source")),
    (dict(state="degraded", mark=False, count=0), ("stopped", "restore_source")),
    (dict(state="restricted"), ("restricted", "wait_tick")),
    (dict(state="restricted", count=4), ("restricted", "wait_tick")),
    (dict(state="restricted", mark=True), ("unknown", "check_status")),
    (dict(state="paused"), ("unknown", "check_status")),
    (dict(state=None), ("unknown", "check_status")),
    (dict(state="degraded", result="accepted", mark=True, count=1),
     ("awaiting_resume", "resume_with_ack")),
    (dict(state="degraded", result="already_ready"), ("stopped", "restore_source")),
]


@pytest.mark.parametrize(("kw", "expected"), TABLE)
def test_every_row_of_the_truth_table(kw, expected):
    assert _g(**kw) == expected


@pytest.mark.parametrize("state", ["degraded", "restricted"])
@pytest.mark.parametrize("result", ["rejected_unprocessed", None])
def test_an_unknown_count_is_never_read_as_zero(state, result):
    phase, action = _g(state=state, result=result, mark=result is None and state == "degraded",
                       count=None)
    if result == "rejected_unprocessed" or (result is None and state == "degraded"):
        assert (phase, action) == ("unknown", "check_status")
    else:
        assert action != "resume"


def test_an_unknown_count_does_not_hide_rows_that_do_not_use_it():
    assert _g(state="degraded", result="rejected_watermark", count=None) == (
        "resume_rejected", "restore_source")
    assert _g(state="degraded", count=None) == ("stopped", "restore_source")
    assert _g(state="restricted", count=None) == ("restricted", "wait_tick")
    assert _g(state="ready", count=None) == ("ready", "none")
    assert _g(state="degraded", request=True, count=None) == ("resume_requested", "wait_tick")


def test_the_function_only_returns_pairs_of_the_published_vocabulary():
    states = ["ready", "restricted", "degraded", "paused", None]
    results = [None, "accepted", "already_ready", "rejected_watermark", "rejected_unprocessed", "x"]
    for failed, state, request, result, mark, count in itertools.product(
            (False, True), states, (False, True), results, (False, True), (None, 0, 5)):
        pair = _g(failed=failed, state=state, request=request, result=result, mark=mark,
                  count=count)
        assert pair in guidance_text.PAIRS, (failed, state, request, result, mark, count)
        # ready / none は、読めていて、行が ready で、要求も無いときだけ
        if pair == ("ready", "none"):
            assert state == "ready" and not failed and not request


def test_a_unknown_state_or_a_failed_read_never_reports_ready():
    for kw in (dict(failed=True, state="ready"), dict(state=None), dict(state="idle"),
               dict(state=5), dict(state="READY")):
        assert _g(**kw)[0] != "ready"


# ---- 読み取り (status に載せる値) ---------------------------------------

def _rig(tmp_path, **kw):
    conn = connect(tmp_path / "guidance.db")
    init_db(conn)
    machine = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                 grace=timedelta(seconds=30), storage_source="mt5-live", **kw)
    return conn, machine


def _set(conn, machine, **cols):
    machine._ensure_row(WED)
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE datafeed_outage_state SET {sets} WHERE id=1", tuple(cols.values()))
    conn.commit()


def _health(conn, machine, *, auto, manual):
    """observe が保存した健全の真偽 (案内はこの値だけで決まる)。"""
    machine._ensure_row(WED)
    outage_facts.save_health(conn, auto_healthy=auto, manual_healthy=manual, now=WED)
    conn.commit()


def _open_position(conn, at):
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market", horizon="swing",
                  status="open", now=at, filled_at=at.isoformat())
    conn.commit()


def _bar(conn, at):
    upsert_cache_bars(conn, [Bar(PAIR, "1m", at, 1, 1, 1, 1, 1)], source="mt5-live")
    conn.commit()


def test_the_snapshot_of_a_machine_that_never_observed_is_ready(tmp_path):
    conn, machine = _rig(tmp_path)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["phase"] == "ready" and snap["next_action"] == "none"
    assert snap["unprocessed_positions"] == "unknown"
    assert snap["resume_last_result"] is None and snap["resume_last_result_at"] is None
    assert set(snap) == {"state", "phase", "next_action", "unprocessed_positions",
                         "resume_last_result", "resume_last_result_at", "auto_resume_possible",
                         "contacts"}
    assert snap["contacts"] == []


def test_a_recorded_request_shows_wait_tick_and_keeps_the_last_result(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, recovered_notified_epoch=3,
         resume_last_result="rejected_watermark", resume_last_result_at=WED.isoformat())
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    snap = machine.guidance_snapshot(WED + timedelta(minutes=2), conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("resume_requested", "wait_tick")
    assert snap["resume_last_result"] == "rejected_watermark"
    assert snap["resume_last_result_at"] == WED.isoformat()


def test_the_phase_is_read_from_the_saved_facts_and_not_from_the_awaiting_mark(tmp_path):
    conn, machine = _rig(tmp_path, auto_resume_when_flat=False)
    _set(conn, machine, state="degraded", epoch=4, recovered_notified_epoch=3)
    _health(conn, machine, auto=True, manual=False)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("stopped", "restore_source")
    _health(conn, machine, auto=False, manual=True)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("awaiting_resume", "resume")


def test_the_current_unprocessed_count_decides_whether_an_acknowledge_is_needed(tmp_path):
    conn, machine = _rig(tmp_path, auto_resume_when_flat=False)
    _set(conn, machine, state="degraded", epoch=4, recovered_notified_epoch=4,
         resume_last_result="rejected_unprocessed", resume_last_result_at=WED.isoformat())
    _health(conn, machine, auto=True, manual=True)
    conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start) "
                 "VALUES (?, ?, ?, ?)", (PAIR, "1m", 4, (WED - timedelta(minutes=31)).isoformat()))
    conn.commit()
    _bar(conn, WED - timedelta(minutes=2))
    _open_position(conn, WED - timedelta(minutes=20))
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["unprocessed_positions"] == 1
    # 古い拒否ではなく、保存した現在の facts で決める
    assert (snap["phase"], snap["next_action"]) == ("awaiting_resume", "resume_with_ack")
    assert snap["resume_last_result"] == "rejected_unprocessed"
    conn.execute("UPDATE orders SET status='closed'")
    conn.commit()
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["unprocessed_positions"] == 0
    assert (snap["phase"], snap["next_action"]) == ("awaiting_resume", "resume")


def test_an_observe_failure_marker_reads_unknown_even_with_a_ready_row(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=1)
    machine._observe_failed = True
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("unknown", "check_status")


def test_an_unreadable_count_is_reported_unknown_not_zero(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path, auto_resume_when_flat=False)
    _set(conn, machine, state="degraded", epoch=4, recovered_notified_epoch=4)
    _health(conn, machine, auto=False, manual=True)

    def boom(*_a, **_k):
        raise RuntimeError("count unreadable")

    monkeypatch.setattr(machine, "_ack_required", boom)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["unprocessed_positions"] == "unknown"
    assert (snap["phase"], snap["next_action"]) == ("unknown", "check_status")


def test_an_unreadable_row_is_unknown_and_never_ready(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("row unreadable")

    monkeypatch.setattr(machine, "_load_row", boom)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("unknown", "check_status")
    assert snap["unprocessed_positions"] == "unknown"


def test_the_snapshot_never_writes_a_row(tmp_path):
    conn, machine = _rig(tmp_path)
    machine.guidance_snapshot(WED, conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM datafeed_outage_state").fetchone()[0] == 0


# ---- 停止の補足 (pending / confirmed)・開場/休場は案内を変えない -------------------

SAT = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)

# (名前, 行の値, 建玉を置くか, 保存した健全 (自動, 手動), 期待)。
# 案内は保存した現在の facts で決まり、古い拒否・待ちの印では決まらない (拒否は別欄)
SCENARIOS = [
    ("ready", dict(state="ready", epoch=3), False, (True, True), ("ready", "none")),
    ("ready+request", dict(state="ready", epoch=3, resume_requested_at=WED.isoformat()), False,
     (True, True), ("resume_requested", "wait_tick")),
    ("degraded+request", dict(state="degraded", epoch=3, resume_requested_at=WED.isoformat(),
                              recovered_notified_epoch=3), False, (True, True),
     ("resume_requested", "wait_tick")),
    ("rejected_watermark", dict(state="degraded", epoch=3, resume_last_result="rejected_watermark",
                                resume_last_result_at=WED.isoformat()), True, (False, False),
     ("stopped", "restore_source")),
    ("rejected_unprocessed+restricted", dict(state="restricted", epoch=3,
                                             resume_last_result="rejected_unprocessed",
                                             resume_last_result_at=WED.isoformat()), True,
     (True, True), ("restricted", "wait_tick")),
    ("rejected_unprocessed+auto", dict(state="degraded", epoch=3,
                                       resume_last_result="rejected_unprocessed",
                                       resume_last_result_at=WED.isoformat()), False,
     (True, True), ("recovering", "wait_tick")),
    ("awaiting+positions", dict(state="degraded", epoch=3, recovered_notified_epoch=3), True,
     (True, True), ("awaiting_resume", "resume_with_ack")),
    ("awaiting+migration", dict(state="degraded", epoch=3, recovered_notified_epoch=3,
                                migration_confirmation_pending=1), False, (True, True),
     ("awaiting_resume", "resume_with_ack")),
    ("stopped", dict(state="degraded", epoch=3), True, (False, False), ("stopped", "restore_source")),
    ("restricted", dict(state="restricted", epoch=3), False, (True, True),
     ("restricted", "wait_tick")),
    ("recovering", dict(state="degraded", epoch=3), False, (True, False),
     ("recovering", "wait_tick")),
    ("recovering-needs-healthy", dict(state="degraded", epoch=3), False, (False, True),
     ("stopped", "restore_source")),
]


@pytest.mark.parametrize("now", [WED, SAT], ids=["weekday", "weekend"])
@pytest.mark.parametrize("confirmed", [0, 1])
@pytest.mark.parametrize("pending", [0, 1])
@pytest.mark.parametrize(("name", "cols", "positions", "health", "expected"), SCENARIOS,
                         ids=[row[0] for row in SCENARIOS])
def test_pending_confirmed_and_the_market_hours_do_not_change_the_guidance(
        tmp_path, name, cols, positions, health, expected, pending, confirmed, now):
    from agentic_fx.ops import client
    conn, machine = _rig(tmp_path)
    _set(conn, machine, pending_human_confirmation=pending, confirmed=confirmed, **cols)
    _health(conn, machine, auto=health[0], manual=health[1])
    if positions:
        conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start) "
                     "VALUES (?, ?, ?, ?)",
                     (PAIR, "1m", 3, (WED - timedelta(minutes=31)).isoformat()))
        _bar(conn, WED - timedelta(minutes=2))
        _open_position(conn, WED - timedelta(minutes=20))
    snap = machine.guidance_snapshot(now, conn=conn)
    assert (snap["phase"], snap["next_action"]) == expected
    # ready の行は件数を数えない
    expected_count = "unknown" if cols["state"] == "ready" else (
        1 if positions or cols.get("migration_confirmation_pending") else 0)
    assert snap["unprocessed_positions"] == expected_count
    # API の data をそのまま ctl に通すと、同じ組の固定文が出る
    feed = {"state": cols["state"], **snap}
    lines = client.format_status({"data": feed}).splitlines()
    assert lines[lines.index(f"data: {cols['state']}") + 1:][:2] == list(
        guidance_text.guidance_lines(feed))


def test_the_snapshot_builds_state_phase_and_count_from_one_read_of_the_row(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=3)
    real = machine._load_row
    reads = []

    def read_then_transition(*a, **k):
        row = real(*a, **k)
        reads.append(row.get("state"))
        # 読んだ直後に別の接続が停止へ遷移させる
        conn.execute("UPDATE datafeed_outage_state SET state='degraded' WHERE id=1")
        conn.commit()
        return row

    machine._load_row = read_then_transition
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert reads == ["ready"]
    assert snap["state"] == "ready"
    assert (snap["phase"], snap["next_action"]) == ("ready", "none")
    machine._load_row = real
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["state"] == "degraded" and snap["phase"] != "ready"


def test_the_snapshot_state_reads_degraded_while_an_observe_failure_marker_is_up(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=1)
    machine._observe_failed = True
    assert machine.guidance_snapshot(WED, conn=conn)["state"] == "degraded"


# ---- 件数を数える条件 ----------------------------------------------------

def _count_calls(machine, monkeypatch):
    """件数の読取りの呼出し回数を記録する (本体の except に吸収されない観測)。"""
    calls = []
    real = machine._ack_required

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(machine, "_ack_required", counted)
    return calls


def test_a_ready_row_does_not_count_positions(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=3)
    calls = _count_calls(machine, monkeypatch)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert calls == []
    assert (snap["phase"], snap["next_action"]) == ("ready", "none")
    assert snap["unprocessed_positions"] == "unknown"


def test_the_call_counter_sees_a_count_for_a_degraded_row(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3)
    calls = _count_calls(machine, monkeypatch)
    machine.guidance_snapshot(WED, conn=conn)
    assert calls == [1]


@pytest.mark.parametrize("state", ["degraded", "restricted"])
def test_a_stopped_or_restricted_row_counts_positions(tmp_path, state):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state=state, epoch=3)
    assert machine.guidance_snapshot(WED, conn=conn)["unprocessed_positions"] == 0


def test_a_ready_row_with_a_rejected_last_result_still_counts(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=3, resume_last_result="rejected_unprocessed",
         resume_last_result_at=WED.isoformat())
    assert machine.guidance_snapshot(WED, conn=conn)["unprocessed_positions"] == 0


# ---- 自動で戻ることがある停止 ----------------------------------------------

def test_a_flat_degraded_epoch_with_auto_resume_and_no_pending_may_recover_unattended(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, pending_human_confirmation=0)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["auto_resume_possible"]) == ("stopped", True)


def test_an_exposure_a_contact_a_migration_wait_or_a_disabled_auto_resume_rules_that_out(
        tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, pending_human_confirmation=1)
    # 通常の pending は自動復帰を妨げない
    assert machine.guidance_snapshot(WED, conn=conn)["auto_resume_possible"] is True
    _open_position(conn, WED - timedelta(minutes=5))
    assert machine.guidance_snapshot(WED, conn=conn)["auto_resume_possible"] is False
    conn.execute("UPDATE orders SET status='closed'")
    conn.commit()
    assert machine.guidance_snapshot(WED, conn=conn)["auto_resume_possible"] is True
    outage_facts.add_contact(conn, epoch=3, order_id=1, pair=PAIR, bar_time=WED, kind="sl",
                             detected_at=WED)
    conn.commit()
    assert machine.guidance_snapshot(WED, conn=conn)["auto_resume_possible"] is False
    conn.execute("DELETE FROM datafeed_outage_contacts")
    _set(conn, machine, migration_confirmation_pending=1)
    assert machine.guidance_snapshot(WED, conn=conn)["auto_resume_possible"] is False
    (tmp_path / "off").mkdir()
    off_conn, off = _rig(tmp_path / "off", auto_resume_when_flat=False)
    _set(off_conn, off, state="degraded", epoch=3, pending_human_confirmation=0)
    assert off.guidance_snapshot(WED, conn=off_conn)["auto_resume_possible"] is False


@pytest.mark.parametrize("cols", [dict(state="ready"), dict(state="restricted")])
def test_only_a_degraded_row_can_report_an_unattended_recovery(tmp_path, cols):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, epoch=3, **cols)
    assert machine.guidance_snapshot(WED, conn=conn)["auto_resume_possible"] is False


def test_an_unreadable_flat_check_does_not_claim_an_unattended_recovery(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3)
    monkeypatch.setattr("agentic_fx.datafeed.outage.orders.list_by_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreadable")))
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["auto_resume_possible"] is False


def test_the_unknown_payload_has_one_definition_and_no_state():
    payload = unknown_guidance()
    assert payload == {"phase": "unknown", "next_action": "check_status",
                       "unprocessed_positions": "unknown", "resume_last_result": None,
                       "resume_last_result_at": None, "auto_resume_possible": False}
    assert unknown_guidance() is not payload


def test_an_unreadable_row_returns_the_shared_unknown_payload(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    monkeypatch.setattr(machine, "_load_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert machine.guidance_snapshot(WED, conn=conn) == unknown_guidance()


# ---- 自動で戻る注記は、確認できた値だけで真にする --------------------------------

GOOD_ROW = {"state": "degraded", "epoch": 3, "migration_confirmation_pending": 0}


def _possible(tmp_path, row, *, auto=True, orders_result=None, monkeypatch=None):
    conn, machine = _rig(tmp_path)
    machine.auto_resume_when_flat = auto
    if orders_result is not None:
        monkeypatch.setattr("agentic_fx.datafeed.outage.orders.list_by_status",
                            lambda *a, **k: orders_result())
    return machine._may_resume_unattended(row, conn)


def test_the_confirmed_values_make_it_true(tmp_path):
    assert _possible(tmp_path, dict(GOOD_ROW)) is True


@pytest.mark.parametrize("migration", [None, "0", "", 1, 2, True, False, 0.0, [], "false"])
def test_a_migration_value_that_is_not_the_integer_zero_is_false(tmp_path, migration):
    row = {"state": "degraded", "epoch": 3, "migration_confirmation_pending": migration}
    assert _possible(tmp_path, row) is False


def test_a_missing_migration_value_is_false(tmp_path):
    assert _possible(tmp_path, {"state": "degraded", "epoch": 3}) is False


@pytest.mark.parametrize("auto", ["false", "true", "", 1, 0, None, [], "no"])
def test_an_auto_resume_setting_that_is_not_the_strict_true_is_false(tmp_path, auto):
    assert _possible(tmp_path, dict(GOOD_ROW), auto=auto) is False


@pytest.mark.parametrize("result", [None, "", (), "none", {}, 0, False, [1], ({"id": 1},)])
def test_a_positions_read_that_is_not_an_empty_list_is_false(tmp_path, monkeypatch, result):
    assert _possible(tmp_path, dict(GOOD_ROW), orders_result=lambda: result,
                     monkeypatch=monkeypatch) is False


def test_an_empty_list_from_the_positions_read_is_true(tmp_path, monkeypatch):
    assert _possible(tmp_path, dict(GOOD_ROW), orders_result=lambda: [],
                     monkeypatch=monkeypatch) is True


def test_a_failed_positions_read_is_false(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("unreadable")

    assert _possible(tmp_path, dict(GOOD_ROW), orders_result=boom, monkeypatch=monkeypatch) is False


# ---- 保存した facts で決める行 (古い拒否・待ちの印は見ない) -----------------------------

def _gf(*, state="degraded", auto_possible=False, auto=None, manual=None, count=0, request=False,
        result=None, mark=False, failed=False):
    return resume_guidance(
        observe_failed=failed, state=state, request_pending=request, last_result=result,
        awaiting_mark=mark, unprocessed=count,
        facts=GuidanceFacts(auto_possible=auto_possible, auto_healthy=auto, manual_healthy=manual))


FACTS_TABLE = [
    # (引数) -> (phase, next_action)
    (dict(state="restricted"), ("restricted", "wait_tick")),
    (dict(state="restricted", result="rejected_unprocessed", count=2),
     ("restricted", "wait_tick")),
    (dict(auto_possible=True, auto=True, manual=True), ("recovering", "wait_tick")),
    (dict(auto_possible=True, auto=True, manual=False), ("recovering", "wait_tick")),
    (dict(auto_possible=True, auto=False, manual=True), ("stopped", "restore_source")),
    (dict(auto_possible=True, auto=None, manual=None), ("stopped", "restore_source")),
    (dict(auto_possible=False, auto=True, manual=True, count=2), ("awaiting_resume", "resume_with_ack")),
    (dict(auto_possible=False, auto=False, manual=True, count=0), ("awaiting_resume", "resume")),
    (dict(auto_possible=False, auto=True, manual=False, count=2), ("stopped", "restore_source")),
    (dict(auto_possible=False, auto=True, manual=None, count=2), ("stopped", "restore_source")),
    # 古い拒否・待ち印は現在の facts より後
    (dict(auto_possible=True, auto=True, manual=True, result="rejected_watermark", mark=True),
     ("recovering", "wait_tick")),
    (dict(auto_possible=False, manual=True, count=1, result="rejected_watermark"),
     ("awaiting_resume", "resume_with_ack")),
    # 要求あり・読取り失敗・ready は facts より先
    (dict(request=True, auto_possible=True, auto=True), ("resume_requested", "wait_tick")),
    (dict(failed=True, auto_possible=True, auto=True), ("unknown", "check_status")),
    (dict(state="ready", auto_possible=True, auto=True), ("ready", "none")),
]


@pytest.mark.parametrize(("kw", "expected"), FACTS_TABLE)
def test_every_row_of_the_facts_truth_table(kw, expected):
    assert _gf(**kw) == expected


def test_an_unknown_count_is_unknown_only_where_the_ack_decides():
    assert _gf(auto_possible=False, manual=True, count=None) == ("unknown", "check_status")
    assert _gf(auto_possible=True, auto=True, count=None) == ("recovering", "wait_tick")
    assert _gf(state="restricted", count=None) == ("restricted", "wait_tick")


def test_the_facts_function_only_returns_published_pairs():
    for state, possible, auto, manual, count in itertools.product(
            ("ready", "restricted", "degraded", "paused"), (False, True), (None, False, True),
            (None, False, True), (None, 0, 3)):
        pair = _gf(state=state, auto_possible=possible, auto=auto, manual=manual, count=count)
        assert pair in guidance_text.PAIRS


def test_the_recovering_phase_has_a_fixed_sentence_and_no_command():
    reason, action = guidance_text.guidance_lines(
        {"phase": "recovering", "next_action": "wait_tick"})
    assert "健全な足の確認を続けています。条件が揃えば自動解除します" in reason
    assert "data resume" not in action


def _contact_rig(tmp_path, **kw):
    conn, machine = _rig(tmp_path, **kw)
    _set(conn, machine, state="degraded", epoch=3)
    return conn, machine


def test_the_snapshot_lists_the_contact_references_and_counts_them_once_with_the_positions(
        tmp_path):
    conn, machine = _contact_rig(tmp_path, auto_resume_when_flat=False)
    _health(conn, machine, auto=True, manual=True)
    conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start) "
                 "VALUES (?, ?, ?, ?)", (PAIR, "1m", 3, (WED - timedelta(minutes=31)).isoformat()))
    _bar(conn, WED - timedelta(minutes=2))
    _open_position(conn, WED - timedelta(minutes=20))
    order_id = conn.execute("SELECT id FROM orders").fetchone()["id"]
    outage_facts.add_contact(conn, epoch=3, order_id=order_id, pair=PAIR, bar_time=WED,
                             kind="sl", detected_at=WED)
    outage_facts.add_contact(conn, epoch=3, order_id=order_id + 100, pair=PAIR, bar_time=WED,
                             kind="unknown", detected_at=WED)
    conn.commit()
    snap = machine.guidance_snapshot(WED + timedelta(minutes=1), conn=conn)
    # 同じ注文は 1 件 (建玉の近似と接触参照の重複を除く) + 別の注文 1 件
    assert snap["unprocessed_positions"] == 2
    assert (snap["phase"], snap["next_action"]) == ("awaiting_resume", "resume_with_ack")
    assert snap["contacts"] == [
        {"order_id": order_id, "bar_time": WED.isoformat(), "kind": "sl"},
        {"order_id": order_id + 100, "bar_time": WED.isoformat(), "kind": "unknown"}]
    assert snap["auto_resume_possible"] is False


def test_a_contact_never_reads_as_resume_without_an_acknowledge_even_with_zero_positions(
        tmp_path):
    conn, machine = _contact_rig(tmp_path)
    _health(conn, machine, auto=True, manual=True)
    outage_facts.add_contact(conn, epoch=3, order_id=9, pair=PAIR, bar_time=WED, kind="limit",
                             detected_at=WED)
    conn.commit()
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("awaiting_resume", "resume_with_ack")


def test_the_snapshot_without_contacts_has_an_empty_reference_list(tmp_path):
    conn, machine = _contact_rig(tmp_path)
    assert machine.guidance_snapshot(WED, conn=conn)["contacts"] == []


def test_an_unreadable_contact_table_is_unknown_not_stopped(tmp_path):
    conn, machine = _contact_rig(tmp_path)
    conn.execute("DROP TABLE datafeed_outage_contacts")
    conn.commit()
    assert machine.guidance_snapshot(WED, conn=conn) == unknown_guidance()


def test_the_contact_lines_are_fixed_words_with_the_order_the_bar_and_the_kind():
    feed = {"contacts": [
        {"order_id": 7, "bar_time": "2026-09-30T10:20:00+00:00", "kind": "sl"},
        {"order_id": 8, "bar_time": "2026-09-30T10:21:00+00:00", "kind": "unknown"}]}
    assert guidance_text.contact_lines(feed) == [
        "接触・不明: 注文 #7 / 足 2026-09-30T10:20:00+00:00 / 損切り価格に接触",
        "接触・不明: 注文 #8 / 足 2026-09-30T10:21:00+00:00 / 接触の有無を判定できません"]
    assert guidance_text.contact_lines(feed, lambda value: "T") == [
        "接触・不明: 注文 #7 / 足 T / 損切り価格に接触",
        "接触・不明: 注文 #8 / 足 T / 接触の有無を判定できません"]


@pytest.mark.parametrize("feed", [None, {}, {"contacts": None}, {"contacts": "x"},
                                  {"contacts": [None, 3, {}]},
                                  {"contacts": [{"order_id": "7", "bar_time": "t", "kind": "sl"}]},
                                  {"contacts": [{"order_id": True, "bar_time": "t", "kind": "sl"}]},
                                  {"contacts": [{"order_id": 1, "bar_time": "t", "kind": "x"}]}])
def test_a_malformed_contact_reference_is_not_shown(feed):
    assert guidance_text.contact_lines(feed) == []


def test_more_than_five_contacts_show_the_first_five_and_the_rest_as_a_count():
    feed = {"contacts": [{"order_id": i, "bar_time": "t", "kind": "tp"} for i in range(1, 9)]}
    lines = guidance_text.contact_lines(feed)
    assert len(lines) == 6 and lines[-1] == "接触・不明: ほか 3 件"

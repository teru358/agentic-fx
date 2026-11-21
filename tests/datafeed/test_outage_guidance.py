"""status の案内 (phase / next_action) の真理値表と、API に載せる読み取り。

期待値は設計の表から手で書いた値。表は上の行ほど優先する。
"""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.datafeed import guidance_text
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine, resume_guidance
from agentic_fx.core.contracts import Bar
from agentic_fx.store import orders
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
    assert snap["unprocessed_positions"] == 0
    assert snap["resume_last_result"] is None and snap["resume_last_result_at"] is None
    assert set(snap) == {"phase", "next_action", "unprocessed_positions", "resume_last_result",
                         "resume_last_result_at"}


def test_a_recorded_request_shows_wait_tick_and_keeps_the_last_result(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, recovered_notified_epoch=3,
         resume_last_result="rejected_watermark", resume_last_result_at=WED.isoformat())
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    snap = machine.guidance_snapshot(WED + timedelta(minutes=2), conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("resume_requested", "wait_tick")
    assert snap["resume_last_result"] == "rejected_watermark"
    assert snap["resume_last_result_at"] == WED.isoformat()


def test_the_awaiting_mark_is_this_epoch_only(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=4, recovered_notified_epoch=3)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("stopped", "restore_source")
    _set(conn, machine, recovered_notified_epoch=4)
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("awaiting_resume", "resume")


def test_the_current_unprocessed_count_decides_whether_an_acknowledge_is_needed(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=4, recovered_notified_epoch=4,
         resume_last_result="rejected_unprocessed", resume_last_result_at=WED.isoformat())
    conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start) "
                 "VALUES (?, ?, ?, ?)", (PAIR, "1m", 4, (WED - timedelta(minutes=31)).isoformat()))
    conn.commit()
    _bar(conn, WED - timedelta(minutes=2))
    _open_position(conn, WED - timedelta(minutes=20))
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["unprocessed_positions"] == 1
    assert (snap["phase"], snap["next_action"]) == ("resume_rejected", "resume_with_ack")
    conn.execute("UPDATE orders SET status='closed'")
    conn.commit()
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert snap["unprocessed_positions"] == 0
    assert (snap["phase"], snap["next_action"]) == ("resume_rejected", "resume")


def test_an_observe_failure_marker_reads_unknown_even_with_a_ready_row(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=1)
    machine._observe_failed = True
    snap = machine.guidance_snapshot(WED, conn=conn)
    assert (snap["phase"], snap["next_action"]) == ("unknown", "check_status")


def test_an_unreadable_count_is_reported_unknown_not_zero(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=4, recovered_notified_epoch=4)

    def boom(*_a, **_k):
        raise RuntimeError("count unreadable")

    monkeypatch.setattr(machine, "_unprocessed_position_count", boom)
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

SCENARIOS = [
    ("ready", dict(state="ready", epoch=3), False, ("ready", "none")),
    ("ready+request", dict(state="ready", epoch=3, resume_requested_at=WED.isoformat()), False,
     ("resume_requested", "wait_tick")),
    ("degraded+request", dict(state="degraded", epoch=3, resume_requested_at=WED.isoformat(),
                              recovered_notified_epoch=3), False, ("resume_requested", "wait_tick")),
    ("rejected_watermark", dict(state="degraded", epoch=3, resume_last_result="rejected_watermark",
                                resume_last_result_at=WED.isoformat()), False,
     ("resume_rejected", "restore_source")),
    ("rejected_unprocessed+positions", dict(state="restricted", epoch=3,
                                            resume_last_result="rejected_unprocessed",
                                            resume_last_result_at=WED.isoformat()), True,
     ("resume_rejected", "resume_with_ack")),
    ("rejected_unprocessed+none", dict(state="degraded", epoch=3,
                                       resume_last_result="rejected_unprocessed",
                                       resume_last_result_at=WED.isoformat()), False,
     ("resume_rejected", "resume")),
    ("awaiting+positions", dict(state="degraded", epoch=3, recovered_notified_epoch=3), True,
     ("awaiting_resume", "resume_with_ack")),
    ("awaiting+none", dict(state="degraded", epoch=3, recovered_notified_epoch=3), False,
     ("awaiting_resume", "resume")),
    ("stopped", dict(state="degraded", epoch=3), True, ("stopped", "restore_source")),
    ("restricted", dict(state="restricted", epoch=3), False, ("restricted", "wait_tick")),
]


@pytest.mark.parametrize("now", [WED, SAT], ids=["weekday", "weekend"])
@pytest.mark.parametrize("confirmed", [0, 1])
@pytest.mark.parametrize("pending", [0, 1])
@pytest.mark.parametrize(("name", "cols", "positions", "expected"), SCENARIOS,
                         ids=[row[0] for row in SCENARIOS])
def test_pending_confirmed_and_the_market_hours_do_not_change_the_guidance(
        tmp_path, name, cols, positions, expected, pending, confirmed, now):
    from agentic_fx.ops import client
    conn, machine = _rig(tmp_path)
    _set(conn, machine, pending_human_confirmation=pending, confirmed=confirmed, **cols)
    if positions:
        conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start) "
                     "VALUES (?, ?, ?, ?)",
                     (PAIR, "1m", 3, (WED - timedelta(minutes=31)).isoformat()))
        _bar(conn, WED - timedelta(minutes=2))
        _open_position(conn, WED - timedelta(minutes=20))
    snap = machine.guidance_snapshot(now, conn=conn)
    assert (snap["phase"], snap["next_action"]) == expected
    assert snap["unprocessed_positions"] == (1 if positions else 0)
    # API の data をそのまま ctl に通すと、同じ組の固定文が出る
    feed = {"state": cols["state"], **snap}
    lines = client.format_status({"data": feed}).splitlines()
    assert lines[lines.index(f"data: {cols['state']}") + 1:][:2] == list(
        guidance_text.guidance_lines(feed))

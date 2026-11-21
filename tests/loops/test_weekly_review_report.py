"""週次の report: 確定した記録だけから作り、親専有で 1 file、書込み失敗は同じ確定から作り直せる。"""
from __future__ import annotations

import os
import stat
from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx import weekly_review_report as rep
from agentic_fx.store import weekly_review as store
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc
END = datetime(2027, 1, 29, 22, tzinfo=UTC)
NOW = datetime(2027, 1, 31, 1, tzinfo=UTC)
SERIES = {"n": 40, "scored": 40, "r_unknown": 0, "wins": 25, "losses": 15, "flats": 0,
          "avg_r": 0.21, "pf": 1.4, "pf_state": "computed", "level": "normal",
          "r_profit": 14.0, "r_loss": 10.0, "pips_sum": 1.0}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "w.db")
    init_db(c)
    yield c
    c.close()


def week(label="2027-01-29", **kw):
    base = dict(week=label, window_end=END, finalized_at=NOW, evaluated_at=NOW,
                status="completed", evaluation="normal", degraded={},
                inventory_state="normal", inventory_reason=None, stopped=[])
    base.update(kw)
    return store.WeekRecord(**base)


def strategy(name="strat", **kw):
    base = dict(name=name, content_hash="ab" * 8, generation_source=0, generation_op_id=3,
                generation_started_at=END - timedelta(weeks=10), state="ok", state_reason=None,
                first_decided_at=END - timedelta(days=60), n=40, wins=25, losses=15, flats=0,
                r_profit=14.0, r_loss=10.0,
                series={"all": SERIES, "accept": SERIES, "reject": {**SERIES, "n": 0,
                                                                     "level": "none"}},
                holes={"abandoned": {"x": 1}, "no_fill": 2, "data_insufficient": 0,
                       "unterminated": 1, "as_of_unknown": 0, "oldest_unterminated_sec": 7200.0,
                       "r_unknown": 0, "time_unreadable": 0, "overdue_unterminated": 0},
                decisions={"total": 40, "accept": 30, "reject": 10, "gate_passed": 28,
                           "reach": {"signals": 50, "decided": 40, "rate": 0.8}},
                concentration={"days": 20, "max_day_count": 4, "max_day_share": 0.1},
                short_term={"weeks": 4, "n": 10, "avg_r": 0.3, "wins": 6, "losses": 4,
                            "comparison": {"recent": {"signals": 10, "decided": 8, "rate": 0.8},
                                           "previous": {"signals": 0, "decided": 0,
                                                        "rate": None}, "comparable": False}},
                approval={"pairs": {"USDJPY": {"trades": 40, "pf": 1.3, "avg_r": 0.2}}},
                sign="above_floor", reasons=[])
    base.update(kw)
    return store.StrategyRow(**base)


def inv(name, state="unused", **kw):
    base = dict(name=name, state=state, origin="staging", conditions=[], proposable=True,
                not_proposable=None)
    base.update(kw)
    return store.InventoryRow(**base)


def test_a_missing_week_has_no_report(conn):
    assert rep.render_report(conn, "2027-01-29") is None


def test_the_report_states_the_week_the_statuses_and_the_machine_scoring_note(conn):
    store.commit_week(conn, week(), [strategy()], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "2027-01-29" in text and "完了" in text and "正常" in text
    assert "機械採点" in text and "楽観側" in text and "実約定を含まず" in text
    assert "採否前に abandoned となった signal は含みません" in text


def test_an_above_floor_strategy_is_called_above_the_floor_only_on_the_machine_scoring(conn):
    store.commit_week(conn, week(), [strategy()], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "機械採点上は床超え" in text
    assert "収益性を保証しません" in text


def test_a_review_reason_turns_an_above_floor_strategy_into_a_check_request(conn):
    store.commit_week(conn, week(), [strategy(reasons=["no_recent_decisions"])], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "要確認" in text and "直近 2 週に採否のある signal がない" in text
    assert "機械採点上は床超え" not in text


def test_a_stop_candidate_keeps_its_label_with_its_reasons(conn):
    store.commit_week(conn, week(), [strategy(sign="stop_candidate",
                                              reasons=["overdue_unterminated"])], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "停止候補" in text and "評価時刻が期限を超えた未終端" in text


def test_the_three_series_the_holes_the_reach_and_the_concentration_are_shown(conn):
    store.commit_week(conn, week(), [strategy()], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "全採否" in text and "accept" in text and "reject" in text
    assert "no_fill=2" in text and "到達率" in text and "集中度" in text
    assert "承認時" in text and "USDJPY" in text


def test_an_undetermined_strategy_shows_its_reason_and_no_numbers(conn):
    store.commit_week(conn, week(evaluation="degraded", degraded={"strategy_undetermined": 1}),
                      [strategy(state="undetermined", state_reason="time_unreadable",
                                sign="indeterminate", series={}, holes={}, decisions={},
                                concentration={}, short_term={}, first_decided_at=None,
                                approval=None, n=0, wins=0, losses=0, r_profit=0.0,
                                r_loss=0.0, generation_started_at=None, generation_source=-1,
                                generation_op_id=-1)], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "劣化" in text and "判定不能" in text and "time_unreadable" in text
    assert "PF=" not in text


def test_the_unused_list_marks_rows_that_are_not_proposable_and_new_ones(conn):
    store.commit_week(conn, week("2027-01-22"), [], [inv("old")])
    store.commit_week(conn, week(), [], [
        inv("old"), inv("fresh"),
        inv("human", proposable=False, not_proposable="human_origin"),
        inv("unk", proposable=False, not_proposable="unknown_origin"),
        inv("ind", state="indeterminate", proposable=False)])
    text = rep.render_report(conn, "2027-01-29")
    assert "提案対象外" in text and "fresh" in text and "新規" in text
    assert "判定不能" in text


def test_the_first_inventory_is_a_list_only_with_no_new_marks(conn):
    store.commit_week(conn, week(), [], [inv("a")])
    text = rep.render_report(conn, "2027-01-29")
    assert "a" in text and "新規" not in text.split("## 未使用")[1]


def test_an_indeterminate_inventory_says_so(conn):
    store.commit_week(conn, week(evaluation="degraded", degraded={"inventory_unavailable": 1},
                                 inventory_state="indeterminate",
                                 inventory_reason="scan_failed"), [], [])
    assert "棚卸しは判定不能" in rep.render_report(conn, "2027-01-29")


def test_the_stopped_names_are_listed(conn):
    store.commit_week(conn, week(stopped=[{"name": "old", "kind": "strategy"}]), [], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "停止済み" in text and "old" in text


def test_a_name_that_is_not_a_plugin_name_is_not_printed_raw(conn):
    store.commit_week(conn, week(stopped=[{"name": "bad\nname`", "kind": None}]), [], [])
    text = rep.render_report(conn, "2027-01-29")
    assert "bad\nname" not in text


def test_an_aborted_week_reports_the_overrun_and_has_no_numbers(conn):
    store.commit_aborted(conn, "2027-01-29", window_end=END, finalized_at=NOW,
                         reason="period_exceeded")
    text = rep.render_report(conn, "2027-01-29")
    assert "打切り" in text and "未評価" in text and "期間超過" in text
    assert "全採否" not in text


def test_the_report_is_written_once_owner_only(conn, tmp_path):
    store.commit_week(conn, week(), [strategy()], [])
    reports = tmp_path / "data" / "improve_reports" / "weekly-review"
    path = rep.ensure_report(conn, "2027-01-29", reports_dir=reports, anchor=tmp_path)
    assert path is not None and path.name == "weekly-review-2027-01-29.md"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(reports.stat().st_mode) == 0o700
    assert rep.ensure_report(conn, "2027-01-29", reports_dir=reports, anchor=tmp_path) is None


def test_a_missing_report_is_regenerated_from_the_same_record(conn, tmp_path):
    store.commit_week(conn, week(), [strategy()], [])
    reports = tmp_path / "data" / "improve_reports" / "weekly-review"
    first = rep.ensure_report(conn, "2027-01-29", reports_dir=reports, anchor=tmp_path)
    body = first.read_text()
    first.unlink()
    again = rep.ensure_report(conn, "2027-01-29", reports_dir=reports, anchor=tmp_path)
    assert again is not None and again.read_text() == body


def test_a_week_without_a_record_writes_no_file(conn, tmp_path):
    reports = tmp_path / "data" / "improve_reports" / "weekly-review"
    assert rep.ensure_report(conn, "2027-01-29", reports_dir=reports, anchor=tmp_path) is None
    assert not reports.exists()


def test_a_symlinked_report_dir_is_refused(conn, tmp_path):
    store.commit_week(conn, week(), [], [])
    target = tmp_path / "elsewhere"
    target.mkdir()
    base = tmp_path / "data" / "improve_reports"
    base.mkdir(parents=True)
    os.symlink(target, base / "weekly-review")
    with pytest.raises(OSError):
        rep.ensure_report(conn, "2027-01-29", reports_dir=base / "weekly-review",
                          anchor=tmp_path)
    assert list(target.iterdir()) == []

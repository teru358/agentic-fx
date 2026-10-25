"""床未達レポートの parser。render -> parse の往復と、形式不正の全体破棄。"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from agentic_fx.loops.improve_loop import ImproveLoop, parse_floor_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "improve_reports"
KEYS = ImproveLoop._FLOOR_REPORT_METRIC_KEYS
HEADER = "| pair | " + " | ".join(KEYS) + " |"
SEPARATOR = "|---" * (len(KEYS) + 1) + "|"
MID = 2043


def _m(trades=20, pf=1.5, avg_r=0.2, evaluable=True):
    return {"trades": trades, "pf": pf, "win_rate": 0.5, "avg_r": avg_r,
            "max_drawdown": 0.05, "total_pnl": 100.0,
            "kill_switch_latches": 0, "evaluable": evaluable}


@pytest.fixture
def strict_loop(loop_min):
    """holdout が評価不能でも落とす設定 (実運用の既定と同じ)。"""
    settings = loop_min._settings
    gate = settings.improve.gate.model_copy(update={"require_holdout_evaluable": True})
    loop_min._settings = settings.model_copy(update={
        "improve": settings.improve.model_copy(update={"gate": gate})})
    return loop_min


def _full(detail: str, mission_id: int = MID, reason: str = "unprofitable") -> str:
    return (f"# Improve Mission {mission_id} — gate failed\n\n"
            f"reason: `{reason}`\n\nNo approval request was produced by "
            "this mission.\n"
            f"\n## Profitability floor detail\n\n{detail}\n")


def _render(loop, in_sample, holdout, mission_id=MID):
    return _full(loop._render_floor_report_detail(
        in_sample_metrics=in_sample, holdout_metrics=holdout), mission_id)


def _summary(detail):
    return None if detail is None else (
        detail.stage, [(p.pair, p.trades, p.pf, p.avg_r, p.evaluable)
                       for p in detail.pairs])


# ---- 往復 ----

def test_roundtrip_in_sample_failure_has_no_holdout_table(loop_min):
    text = _render(loop_min, {"USDJPY": _m(trades=30, pf=0.8, avg_r=-0.1)}, {})
    assert "### holdout" not in text
    assert _summary(parse_floor_report(text, mission_id=MID)) == (
        "in_sample", [("USDJPY", 30, 0.8, -0.1, True)])


def test_roundtrip_holdout_failure_returns_the_holdout_rows(strict_loop):
    text = _render(
        strict_loop, {"USDJPY": _m(pf=1.2)},
        {"USDJPY": _m(trades=24, pf=0.51, avg_r=-0.27, evaluable=False)})
    assert _summary(parse_floor_report(text, mission_id=MID)) == (
        "holdout", [("USDJPY", 24, 0.51, -0.27, False)])


def test_roundtrip_multiple_pairs_keeps_table_order_and_only_failed_pairs(loop_min):
    in_sample = {p: _m() for p in ("USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "XAUUSD")}
    holdout = {"USDJPY": _m(trades=35, pf=0.7, avg_r=-0.2),
               "EURUSD": _m(trades=38, pf=1.2, avg_r=0.15),
               "GBPUSD": _m(trades=40, pf=0.9, avg_r=-0.05),
               "AUDUSD": _m(trades=41, pf=0.6, avg_r=-0.3),
               "XAUUSD": _m(trades=42, pf=0.95, avg_r=0.1)}
    text = _render(loop_min, in_sample, holdout)
    detail = parse_floor_report(text, mission_id=MID)
    assert _summary(detail) == ("holdout", [
        ("USDJPY", 35, 0.7, -0.2, True), ("GBPUSD", 40, 0.9, -0.05, True),
        ("AUDUSD", 41, 0.6, -0.3, True), ("XAUUSD", 42, 0.95, 0.1, True)])


def test_roundtrip_none_pf_and_avg_r_become_none(strict_loop):
    text = _render(
        strict_loop, {"USDJPY": _m()},
        {"USDJPY": _m(trades=5, pf=None, avg_r=None, evaluable=False)})
    assert _summary(parse_floor_report(text, mission_id=MID)) == (
        "holdout", [("USDJPY", 5, None, None, False)])


def test_roundtrip_both_stages_failing_is_rejected(loop_min):
    text = _render(loop_min, {"USDJPY": _m(pf=0.5)}, {"USDJPY": _m(pf=0.5)})
    assert "failed_stage: in_sample, holdout" in text
    assert parse_floor_report(text, mission_id=MID) is None


def test_reason_and_thresholds_lines_do_not_change_the_output(loop_min):
    base = _render(loop_min, {"USDJPY": _m()}, {"USDJPY": _m(pf=0.5)})
    want = _summary(parse_floor_report(base, mission_id=MID))
    other_reason = base.replace("reason: `unprofitable`", "reason: `whatever 9.99`")
    other_thresholds = base.replace("thresholds: ", "thresholds: x=1 ")
    assert _summary(parse_floor_report(other_reason, mission_id=MID)) == want
    assert _summary(parse_floor_report(other_thresholds, mission_id=MID)) == want


# ---- 実レポート ----

def _real(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_real_report_2043():
    detail = parse_floor_report(_real("improve-2026-10-25-2043.md"), mission_id=2043)
    assert _summary(detail) == ("holdout", [
        ("USDJPY", 13, 1.8458377992140615, 0.3029234868346611, False)])


def test_real_report_2045():
    detail = parse_floor_report(_real("improve-2026-10-25-2045.md"), mission_id=2045)
    assert _summary(detail) == ("holdout", [
        ("USDJPY", 24, 0.5145606198869912, -0.2772392430083411, False)])


def test_real_report_with_another_mission_id_is_rejected():
    assert parse_floor_report(_real("improve-2026-10-25-2043.md"),
                              mission_id=2044) is None


# ---- 形式不正は全体を捨てる ----

def _base(loop):
    return _render(loop, {"USDJPY": _m(trades=30)},
                   {"USDJPY": _m(trades=13, pf=0.9, avg_r=-0.1, evaluable=True)})


def _row(pair="USDJPY", trades="13", pf="0.9", avg_r="-0.1", evaluable="True"):
    cells = [pair, trades, pf, "0.5", avg_r, "0.05", "100.0", "0", evaluable]
    return "| " + " | ".join(cells) + " |"


def _holdout_row(text):
    return [line for line in text.split("\n") if line.startswith("| USDJPY")][-1]


def _replace_holdout_row(text, new):
    old = _holdout_row(text)
    head, _, tail = text.rpartition(old)
    return head + new + tail


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("# Improve Mission 2043 — gate failed",
                        "# Improve Mission 2044 — gate failed"),
    lambda t: t.replace("—", "-"),
    lambda t: t.replace("## Profitability floor detail\n", ""),
    lambda t: t.replace("## Profitability floor detail", "## Profitability floor"),
    lambda t: t.replace("failed_stage: holdout", "failed_stage: "),
    lambda t: t.replace("failed_stage: holdout", "failed_stage: in_sample, holdout"),
    lambda t: t.replace("failed_stage: holdout", "failed_stage: in_sample,holdout"),
    lambda t: t.replace("failed_stage: holdout", "failed_stage: weekly"),
    lambda t: t.replace("failed_pairs: USDJPY", "failed_pairs: "),
    lambda t: t.replace("failed_pairs: USDJPY", "failed_pairs: usdjpy"),
    lambda t: t.replace("failed_pairs: USDJPY", "failed_pairs: US"),
    lambda t: t.replace("failed_pairs: USDJPY", "failed_pairs: USDJPY, USDJPY"),
    lambda t: t.replace("failed_pairs: USDJPY", "failed_pairs: EURUSD"),
    lambda t: t.replace("thresholds:", "limits:"),
    lambda t: t.replace("No approval request was produced by this mission.",
                        "An approval request was produced."),
    lambda t: t.replace("### in_sample", "### in-sample"),
    lambda t: t.replace("### holdout", "### holdout_x"),
    lambda t: t.replace(HEADER, HEADER.replace("pf", "pf2")),
    lambda t: t.replace(HEADER, HEADER.replace("| avg_r", "| avg_r | extra")),
    lambda t: t.replace(SEPARATOR, SEPARATOR[:-4] + "|"),
    lambda t: t.replace(SEPARATOR, SEPARATOR.replace("---", "--", 1)),
    lambda t: t.rpartition("### holdout")[0],
    lambda t: _replace_holdout_row(t, _row(trades="-1")),
    lambda t: _replace_holdout_row(t, _row(trades="1.5")),
    lambda t: _replace_holdout_row(t, _row(trades="True")),
    lambda t: _replace_holdout_row(t, _row(trades="")),
    lambda t: _replace_holdout_row(t, _row(pf="nan")),
    lambda t: _replace_holdout_row(t, _row(pf="inf")),
    lambda t: _replace_holdout_row(t, _row(pf="-inf")),
    lambda t: _replace_holdout_row(t, _row(pf="")),
    lambda t: _replace_holdout_row(t, _row(pf="1e999")),
    lambda t: _replace_holdout_row(t, _row(pf="1_0")),
    lambda t: _replace_holdout_row(t, _row(avg_r="nan")),
    lambda t: _replace_holdout_row(t, _row(avg_r="")),
    lambda t: _replace_holdout_row(t, _row(avg_r="none")),
    lambda t: _replace_holdout_row(t, _row(evaluable="true")),
    lambda t: _replace_holdout_row(t, _row(evaluable="1")),
    lambda t: _replace_holdout_row(t, _row(evaluable="")),
    lambda t: _replace_holdout_row(t, _row()[:-2]),
    lambda t: _replace_holdout_row(t, _row() + "  "),
    lambda t: _replace_holdout_row(t, _row(pair="usdjpy")),
    lambda t: _replace_holdout_row(t, _row(pair="EURUSD")),
    lambda t: _replace_holdout_row(t, _row() + "\n" + _row()),
    lambda t: t.replace("reason: `unprofitable`", "reason: `unprofitable`\nextra line"),
])
def test_malformed_report_is_rejected_entirely(loop_min, mutate):
    text = _base(loop_min)
    assert parse_floor_report(text, mission_id=MID) is not None
    mutated = mutate(text)
    assert mutated != text
    assert parse_floor_report(mutated, mission_id=MID) is None


def test_in_sample_table_is_required_even_when_holdout_fails(loop_min):
    text = _base(loop_min)
    head, _, tail = text.partition("### in_sample")
    _, _, holdout = tail.partition("### holdout")
    assert parse_floor_report(head + "### holdout" + holdout, mission_id=MID) is None


def test_holdout_table_is_ignored_when_in_sample_failed(loop_min):
    text = _render(loop_min, {"USDJPY": _m(pf=0.5)}, {"USDJPY": _m()})
    assert "failed_stage: in_sample" in text
    clean = ("in_sample", [("USDJPY", 20, 0.5, 0.2, True)])
    assert _summary(parse_floor_report(text, mission_id=MID)) == clean
    before, _, _ = text.partition("### holdout")
    with_holdout = before + "### holdout\n\nnot a table\nnan nan\n"
    assert with_holdout != text
    assert _summary(parse_floor_report(with_holdout, mission_id=MID)) == clean
    assert _summary(parse_floor_report(before.rstrip("\n") + "\n",
                                       mission_id=MID)) == clean


def test_in_sample_table_is_validated_when_holdout_fails(loop_min):
    text = _base(loop_min)
    first = [line for line in text.split("\n") if line.startswith("| USDJPY")][0]
    bad = first.replace("| 30 |", "| -3 |", 1)
    assert bad != first
    assert parse_floor_report(text.replace(first, bad, 1), mission_id=MID) is None


@pytest.mark.parametrize("value", [None, "", 3, b"x", "# Improve Mission 2043"])
def test_non_report_input_returns_none(value):
    assert parse_floor_report(value, mission_id=MID) is None


@pytest.mark.parametrize("mission_id", [True, "2043", None, 2043.0])
def test_non_int_mission_id_returns_none(loop_min, mission_id):
    assert parse_floor_report(_base(loop_min), mission_id=mission_id) is None


def test_parse_is_pure_and_does_not_raise_on_garbage():
    for text in ("\x00" * 10, "\n" * 5, "# Improve Mission 1 — gate failed\n" * 3,
                 "## Profitability floor detail\n" * 3):
        assert parse_floor_report(text, mission_id=1) is None


def test_values_are_finite_floats():
    detail = parse_floor_report(_real("improve-2026-10-25-2043.md"), mission_id=2043)
    assert all(isinstance(p.pf, float) and math.isfinite(p.pf) for p in detail.pairs)

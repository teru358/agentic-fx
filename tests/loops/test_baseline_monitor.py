import pytest

from agentic_fx.loops.baseline_monitor import summarize_comparisons


def test_monitor_schema_and_strict_monotone_run():
    records = [
        {"strategy": "s", "candidate": "a" * 64, "base": "b" * 64,
         "verdict": "improved", "not_submitted": False, "pf": 1.1, "avg_r": 0.1,
         "window_overlap": 0.5},
        {"strategy": "s", "candidate": "c" * 64, "base": "b" * 64,
         "verdict": "not_improved", "not_submitted": True, "pf": 1.2, "avg_r": 0.2,
         "window_overlap": None},
    ]
    assert summarize_comparisons(records) == [{
        "strategy": "s", "attempts": 2,
        "by_candidate_hash": [
            {"candidate": "a" * 12, "base": "b" * 12, "verdict": "improved", "monotone_run": 1},
            {"candidate": "c" * 12, "base": "b" * 12, "verdict": "not_improved", "monotone_run": 2}],
        "not_submitted_share": 0.5, "unavailable_rate": 0.0,
        "window_overlap": {"mean": 0.5, "n": 1}}]


def test_monitor_equal_metric_breaks_strict_monotone_run():
    rows = summarize_comparisons([
        {"strategy": "s", "candidate": "a" * 64, "base": "b" * 64,
         "verdict": "improved", "not_submitted": False, "pf": 1.1, "avg_r": 0.1,
         "window_overlap": None},
        {"strategy": "s", "candidate": "c" * 64, "base": "b" * 64,
         "verdict": "improved", "not_submitted": False, "pf": 1.1, "avg_r": 0.2,
         "window_overlap": None},
    ])
    assert rows[0]["by_candidate_hash"][1]["monotone_run"] == 1


def _rec(candidate, pf, avg_r, **over):
    return {"strategy": "s", "candidate": candidate * 64, "base": "b" * 64,
            "verdict": "improved", "not_submitted": False, "pf": pf, "avg_r": avg_r,
            "window_overlap": None, **over}


def test_monitor_run_resets_when_the_direction_flips():
    rows = summarize_comparisons([_rec("a", 1.0, 0.1), _rec("b", 1.1, 0.2), _rec("c", 1.0, 0.1),
                                  _rec("d", 0.9, 0.0)])
    assert [h["monotone_run"] for h in rows[0]["by_candidate_hash"]] == [1, 2, 1, 2]


def test_monitor_one_metric_moving_alone_is_not_monotone():
    rows = summarize_comparisons([_rec("a", 1.0, 0.1), _rec("b", 1.1, 0.1)])
    assert rows[0]["by_candidate_hash"][1]["monotone_run"] == 1


def test_monitor_missing_metrics_do_not_extend_a_run_and_do_not_fail():
    rows = summarize_comparisons([_rec("a", None, None), _rec("b", 1.1, 0.2)])
    assert rows[0]["by_candidate_hash"][1]["monotone_run"] == 1


def test_monitor_rates_and_overlap_mean_exclude_null_overlaps_from_n():
    rows = summarize_comparisons([
        _rec("a", 1.0, 0.1, window_overlap=0.2),
        _rec("b", 1.1, 0.2, verdict="unavailable", not_submitted=True, window_overlap=0.4),
        _rec("c", 1.2, 0.3, verdict="not_improved", not_submitted=True)])
    (row,) = rows
    assert row["attempts"] == 3
    assert row["not_submitted_share"] == pytest.approx(2 / 3)
    assert row["unavailable_rate"] == pytest.approx(1 / 3)
    assert row["window_overlap"]["n"] == 2
    assert row["window_overlap"]["mean"] == pytest.approx(0.3)


def test_monitor_groups_by_strategy():
    rows = summarize_comparisons([_rec("a", 1.0, 0.1), _rec("b", 1.1, 0.2, strategy="t")])
    assert [r["strategy"] for r in rows] == ["s", "t"]
    assert all(r["attempts"] == 1 for r in rows)


def test_monitor_schema_keys_are_exact():
    (row,) = summarize_comparisons([_rec("a", 1.0, 0.1)])
    assert set(row) == {"strategy", "attempts", "by_candidate_hash", "not_submitted_share",
                        "unavailable_rate", "window_overlap"}
    assert set(row["by_candidate_hash"][0]) == {"candidate", "base", "verdict", "monotone_run"}
    assert set(row["window_overlap"]) == {"mean", "n"}


def test_the_report_section_lists_counts_and_hashes_and_handles_no_history():
    from agentic_fx.loops.baseline_monitor import render_monitor_section
    assert "No comparison attempts recorded." in render_monitor_section([])
    text = render_monitor_section(summarize_comparisons([_rec("a", 1.0, 0.1)]))
    assert "- s: attempts=1" in text and "window_overlap mean=null n=0" in text
    assert "candidate=" + "a" * 12 in text and "monotone_run=1" in text

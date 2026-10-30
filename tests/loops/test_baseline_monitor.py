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

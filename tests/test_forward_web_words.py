"""機械採点の web 表示が使う写し (窓・理由の語・読む field) と、daemon の定義の一致。"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.plugin import usage_review
from agentic_fx.store import signal_forward as sf
from agentic_fx.store.db import SIGNAL_ABANDON_REASONS

PAGES = Path(__file__).resolve().parents[1] / "afx-ops" / "afx_ops" / "web" / "perf_pages.py"
NOW = datetime(2027, 1, 31, 12, 0, tzinfo=timezone.utc)


def _literals() -> dict:
    found = {}
    for node in ast.parse(PAGES.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith(
                    ("FORWARD_", "ABANDON_")):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return found


def _real_row() -> dict:
    target = sf.ForwardTarget("alpha", "a" * 64, NOW - timedelta(weeks=6), (0, 3))
    return sf.summarize([], [], target=target, start=NOW - timedelta(weeks=6), end=NOW)


def test_the_copied_window_matches_the_daemon_definition():
    assert _literals()["FORWARD_WINDOW_WEEKS"] == sf.WINDOW_WEEKS


def test_every_abandon_reason_of_the_daemon_has_words_except_the_two_cleanup_kinds():
    assert set(_literals()["ABANDON_LABELS"]) == set(SIGNAL_ABANDON_REASONS) - {
        "ineligible_cleanup", "legacy_unknown"}


def test_every_reason_the_daemon_can_give_has_words():
    words = set(_literals()["FORWARD_REASONS"])
    assert {usage_review.REASON_NO_GENERATION, usage_review.REASON_GENERATION_MISMATCH,
            usage_review.REASON_TIME_UNREADABLE, usage_review.REASON_JOURNAL_SOURCE,
            usage_review.REASON_STRATEGY_UNREADABLE, "summary_failed"} <= words


def test_the_fields_the_page_reads_exist_in_a_real_row():
    row = _real_row()
    for path in (("name",), ("content_hash",), ("state",), ("generation", "started_at"),
                 ("window", "start"), ("thresholds", "min_count"),
                 ("thresholds", "reference_count"), ("decisions", "accept"),
                 ("decisions", "reject"), ("decisions", "gate_passed"),
                 ("reach", "signals"), ("reach", "decided"), ("reach", "rate"),
                 ("holes", "abandoned"), ("holes", "no_fill"), ("holes", "data_insufficient"),
                 ("holes", "unterminated"), ("holes", "oldest_unterminated_sec"),
                 ("holes", "r_unknown"), ("holes", "as_of_unknown"), ("concentration", "days"),
                 ("concentration", "max_day_count"), ("concentration", "max_day_share"),
                 ("comparison", "recent"), ("comparison", "previous"),
                 ("comparison", "comparable")) + tuple(
            ("series", key, field) for key in ("all", "accept", "reject")
            for field in ("n", "scored", "wins", "losses", "flats", "avg_r", "pf", "pf_state")):
        node = row
        for key in path:
            assert key in node, path
            node = node[key]

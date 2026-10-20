"""bot と notify が共有する /events page の読み方。"""
from __future__ import annotations

from pathlib import Path

import afx_ops
from afx_ops import events_page
from afx_ops.api_client import Reply
from afx_ops.bot import adapter
from afx_ops.notify import runtime

ROOT = Path(afx_ops.__file__).parent


def test_the_page_reader_is_defined_once_and_both_consumers_use_it():
    for path in (ROOT / "bot" / "adapter.py", ROOT / "notify" / "runtime.py"):
        assert "def _check_watermark" not in path.read_text(encoding="utf-8")
    assert adapter.parse_page is events_page.parse_page
    assert runtime.parse_page is events_page.parse_page


def test_the_effective_watermark_is_the_larger_of_the_reply_and_the_largest_readable_id():
    reply = Reply(200, {"events": [{"id": 7}, {"id": 3}], "high_watermark": 5}, None)
    events, high, unreadable = events_page.parse_page(reply, ValueError)
    assert ([e["id"] for e in events], high, unreadable) == ([3, 7], 7, 0)


def test_events_from_the_first_unreadable_one_on_are_counted_not_returned():
    reply = Reply(200, {"events": [{"id": 2}, {"id": "x"}, {"id": 9}], "high_watermark": 1}, None)
    events, high, unreadable = events_page.parse_page(reply, ValueError)
    assert ([e["id"] for e in events], high, unreadable) == ([2], 2, 2)

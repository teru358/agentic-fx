"""改善 event が events の page に混じっても、読む側が落ちない。web は events を読まない。"""
from __future__ import annotations

import ast
from pathlib import Path

import afx_ops.web as web_package
from afx_ops.api_client import NotifyClient, Reply, WebClient
from afx_ops.events_page import parse_page

TS = "2026-10-25T00:00:00+00:00"


def _improve(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "improve_run_changed", "ref": "improve:2040",
            "fields": {"state": "finished", "trigger": "manual", "result_code": "observation",
                       "reason_code": None, "tool_calls": 11}}


def test_a_page_with_improve_events_is_parsed_in_id_order():
    page = {"events": [_improve(5), _improve(3)], "high_watermark": 5}

    events, high, unreadable = parse_page(Reply(200, page, None), RuntimeError)

    assert [event["id"] for event in events] == [3, 5] and (high, unreadable) == (5, 0)


def test_the_web_package_neither_reads_events_nor_the_improve_endpoint():
    names = set()
    for path in Path(web_package.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                  for a in n.names}
    assert not names & {"events", "parse_page", "encode_event", "improve_run"}
    assert not hasattr(WebClient, "events") and not hasattr(WebClient, "improve_run")
    assert not hasattr(NotifyClient, "improve_run")

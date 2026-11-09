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


def _web_trees():
    for path in Path(web_package.__file__).parent.glob("*.py"):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"))


def _names(tree) -> set[str]:
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return names


def test_the_web_package_never_reads_events_or_an_improve_run():
    """web は event も改善 run の読取り (`GET /v1/improve/runs*`) も持たない。"""
    for name, tree in _web_trees():
        assert not _names(tree) & {"events", "parse_page", "encode_event"}, name
        strings = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not [text for text in strings if "/v1/improve" in text], name
        if name != "write.py":
            assert "improve_run" not in _names(tree), name
    assert not hasattr(WebClient, "events") and not hasattr(WebClient, "improve_run")


def test_the_only_improve_call_in_web_is_the_write_clients_idempotent_start():
    """改善に触れてよいのは書込みの client の開始 (`improve_run(actor, body, idempotency_key=)`) だけ。"""
    trees = dict(_web_trees())
    tree = trees["write.py"]
    protocol = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == "WriteClient")
    declared = [f for f in protocol.body if isinstance(f, ast.FunctionDef)
                and f.name == "improve_run"]
    assert len(declared) == 1
    assert [a.arg for a in declared[0].args.args] == ["self", "actor", "body"]
    assert [a.arg for a in declared[0].args.kwonlyargs] == ["idempotency_key"]
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "improve_run"]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "improve_run"]
    assert len(uses) == len(calls) >= 1
    assert all([k.arg for k in call.keywords] == ["idempotency_key"] for call in calls)
    assert not hasattr(NotifyClient, "improve_run")

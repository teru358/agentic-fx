"""discoveries は note のみを作り、open の行を作らない (AC-1〜4)。

agent が backlog に新規 task を足せる経路は、その mission で直ちに選択される
`selected.idea` だけ。ここでは discoveries 経路の遮断と、open を作る・戻す
writer の列挙を pin する。
"""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

NOW = datetime(2026, 8, 22)
SRC = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"

_KINDS_SIX = ["task", "fact", "idea", None, "task", "idea"]


def _disc(idea: str, kind: str | None) -> dict:
    d = {"idea": idea, "source": "agent", "evidence": "e"}
    if kind is not None:
        d["kind"] = kind
    return d


def _output(discoveries: list[dict], backlog_id) -> dict:
    return {"discoveries": discoveries,
            "selected": {"backlog_id": backlog_id, "idea": "x"},
            "artifact": {"type": "observation", "reason": "x"},
            "selection_rationale": "x"}


def _open_count(conn) -> int:
    return conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE status='open'"
    ).fetchone()["c"]


def test_ac1_discoveries_never_create_open_rows(loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    before_open = _open_count(conn)
    discoveries = [_disc(f"hypothesis {i}", k)
                   for i, k in enumerate(_KINDS_SIX[:5])]

    loop._select_and_bind(conn, _output(discoveries, backlog_id), ctx, now=NOW)

    rows = conn.execute(
        "SELECT status FROM improvement_backlog WHERE idea LIKE 'hypothesis %'"
    ).fetchall()
    assert len(rows) == 5
    assert {r["status"] for r in rows} == {"note"}
    # 選択で open が 1 件 selected に変わる分だけ減る。増えてはいけない
    assert _open_count(conn) == before_open - 1


@pytest.mark.parametrize("kind", ["task", "fact", "idea", None])
def test_ac1_every_kind_is_stored_as_note(loop_and_ctx_with_open_backlog, kind):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    loop._select_and_bind(
        conn, _output([_disc("one discovery", kind)], backlog_id), ctx, now=NOW)
    row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE idea='one discovery'"
    ).fetchone()
    assert row["status"] == "note"


def test_ac2_task_discovery_does_not_promote_existing_note(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    conn.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm) "
        "VALUES ('same idea','agent','note',?,?, 'same idea')",
        (NOW.isoformat(), NOW.isoformat()))
    conn.commit()

    loop._select_and_bind(
        conn, _output([_disc("same idea", "task")], backlog_id), ctx, now=NOW)

    rows = conn.execute(
        "SELECT status,last_result FROM improvement_backlog "
        "WHERE idea_norm='same idea'").fetchall()
    assert [dict(r) for r in rows] == [{"status": "note", "last_result": None}]


def test_ac2_selected_idea_does_not_promote_existing_note(
        loop_and_ctx_with_open_backlog):
    """selected.idea は同文の note を open に昇格させない。昇格は人間だけ。"""
    loop, ctx, conn, _ = loop_and_ctx_with_open_backlog
    conn.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm) "
        "VALUES ('noted','agent','note',?,?, 'noted')",
        (NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    out = _output([], None)
    out["selected"] = {"backlog_id": None, "idea": "noted"}

    outcome = loop._select_and_bind(conn, out, ctx, now=NOW)

    assert outcome.won is False
    assert conn.execute(
        "SELECT status FROM improvement_backlog WHERE idea_norm='noted'"
    ).fetchone()["status"] == "note"


def test_ac1_selected_idea_is_the_only_new_task_route(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, _ = loop_and_ctx_with_open_backlog
    out = _output([_disc("side note", "task")], None)
    out["selected"] = {"backlog_id": None, "idea": "fresh hypothesis"}

    outcome = loop._select_and_bind(conn, out, ctx, now=NOW)

    assert outcome.won is True
    statuses = {r["idea"]: r["status"] for r in conn.execute(
        "SELECT idea,status FROM improvement_backlog "
        "WHERE idea IN ('side note','fresh hypothesis')")}
    assert statuses == {"side note": "note", "fresh hypothesis": "selected"}


@pytest.mark.parametrize("count,dropped", [(4, 0), (5, 0), (6, 1)])
def test_ac4_discoveries_input_cap_is_five(
        loop_and_ctx_with_open_backlog, tmp_path, count, dropped):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    discoveries = [_disc(f"cap-{i}", "idea") for i in range(count)]

    loop._select_and_bind(conn, _output(discoveries, backlog_id), ctx, now=NOW)

    kept = [r["idea"] for r in conn.execute(
        "SELECT idea FROM improvement_backlog WHERE idea LIKE 'cap-%' ORDER BY id")]
    assert kept == [f"cap-{i}" for i in range(min(count, 5))]
    text = (tmp_path / "activity.log").read_text() \
        if (tmp_path / "activity.log").exists() else ""
    marker = f"mission={ctx.mission_id} dropped={dropped}"
    if dropped:
        assert "backlog_limit_exceeded" in text and marker in text
    else:
        assert "backlog_limit_exceeded" not in text


def test_ac4_duplicates_inside_the_cap_still_count_toward_it(
        loop_and_ctx_with_open_backlog):
    """入力件数の上限なので、重複で skip される件も数に入る。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    discoveries = [_disc("dup", "fact")] * 5 + [_disc("late", "fact")]
    loop._select_and_bind(conn, _output(discoveries, backlog_id), ctx, now=NOW)
    assert conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE idea='late'"
    ).fetchone()["c"] == 0


def _enclosing_writers(pattern: str) -> set[tuple[str, str]]:
    """src 配下で SQL 文字列定数に `pattern` を含む箇所の (相対パス, 関数名)。"""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and pattern in node.value):
                cur: ast.AST | None = node
                while cur is not None and not isinstance(
                        cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cur = parents.get(cur)
                found.add((str(path.relative_to(SRC)),
                           cur.name if cur is not None else "<module>"))
    return found


def test_ac3_backlog_insert_writers_are_exactly_the_allowlist():
    assert _enclosing_writers("INSERT INTO improvement_backlog") == {
        # 親の system note (人間でも agent でもない)
        ("store/backlog.py", "upsert_system_note"),
        # 人間の `afx ctl backlog add`
        ("store/backlog.py", "add"),
        # discoveries の note と、selected.idea の新規 task
        ("loops/improve_loop.py", "_upsert_backlog_idea"),
    }


def test_ac3_backlog_status_update_writers_are_exactly_the_allowlist():
    assert _enclosing_writers("UPDATE improvement_backlog SET status") == {
        ("store/backlog.py", "set_status"),
        ("store/backlog.py", "transition"),
        ("store/backlog.py", "select_for_mission"),
        ("loops/improve_loop.py", "compensate_commit_failure"),
        ("loops/improve_loop.py", "_fail_report"),
        ("store/missions.py", "recover_interrupted"),
    }


def test_ac3_no_agent_path_writes_open_status_by_update():
    """improve_loop は status を open にする UPDATE を持たない (昇格の撤去)。"""
    src = (SRC / "loops" / "improve_loop.py").read_text(encoding="utf-8")
    assert "SET status='open'" not in src
    assert "promoted_from_note" not in src

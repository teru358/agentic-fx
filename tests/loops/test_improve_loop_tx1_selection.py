"""Tx-1: discoveries + backlog CAS + run bind (設計書 §4.1/§4.2-2、
プラン §8.1-24)。"""
from __future__ import annotations

from datetime import datetime

import pytest


def test_winner_gets_backlog_bound_to_run(loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    output = {"discoveries": [], "selected": {"backlog_id": backlog_id, "idea": "x"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    outcome = loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))
    assert outcome.won is True
    row = conn.execute("SELECT status FROM improvement_backlog WHERE id=?",
                       (backlog_id,)).fetchone()
    assert row["status"] == "selected"
    run = conn.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                       (ctx.run_id,)).fetchone()
    assert run["backlog_id"] == backlog_id


def test_loser_run_stays_unbound_but_discoveries_persist(
        loop_and_ctx_with_open_backlog):
    """2 接続同時選択: 先着が勝ち、後着 (敗者) は `backlog_id=NULL` の
    まま — しかし discoveries は両者とも残る (敗者経路の pin)。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    now = datetime(2026, 8, 22)
    # 先着 (別接続を模す): 直接 CAS を先に成功させておく
    conn.execute("UPDATE improvement_backlog SET status='selected' WHERE id=?",
                (backlog_id,))
    conn.commit()

    output = {"discoveries": [{"idea": "new finding", "source": "agent",
                               "evidence": "e"}],
              "selected": {"backlog_id": backlog_id, "idea": "x"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    outcome = loop._select_and_bind(conn, output, ctx, now=now)
    assert outcome.won is False
    run = conn.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                       (ctx.run_id,)).fetchone()
    assert run["backlog_id"] is None
    disc = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE idea='new finding'"
    ).fetchone()["c"]
    assert disc == 1  # discoveries の INSERT は敗者でも残る


def test_duplicate_idea_normalized_whitespace_and_case_is_deduped(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    # 大文字小文字が混在した既存 idea を作成
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at) VALUES ('RSI Divergence', 'agent', 'open', ?, ?)",
        (datetime(2026, 8, 1).isoformat(),) * 2)
    conn.commit()
    # 別の大文字小文字 + 前後空白の discovery を投入
    output = {"discoveries": [{"idea": "  RSI DIVERGENCE  ", "source": "agent",
                               "evidence": "e"}],
              "selected": {"backlog_id": None, "idea": "rsi divergence"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))
    # 前後空白と大文字小文字の違いのみで、正規化後は重複として扱われる
    n = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "lower(trim(idea))='rsi divergence'").fetchone()["c"]
    assert n == 1  # 重複が追加されていない


def test_new_backlog_over_limit_are_dropped_and_counted_in_activity(
        loop_and_ctx_with_open_backlog, monkeypatch):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    monkeypatch.setattr(loop._settings.improve, "max_new_backlog_per_mission", 2)
    discoveries = [{"idea": f"idea-{i}", "source": "agent", "evidence": "e"}
                  for i in range(5)]
    output = {"discoveries": discoveries,
              "selected": {"backlog_id": None, "idea": "idea-0"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))
    n = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE idea LIKE 'idea-%'"
    ).fetchone()["c"]
    assert n == 2


@pytest.mark.parametrize("selected", [
    {"backlog_id": None},
    {"backlog_id": None, "idea": ""},
    {"backlog_id": None, "idea": "   "},
], ids=["idea_key_missing", "idea_empty_string", "idea_whitespace_only"])
def test_empty_selected_idea_is_not_bound_and_creates_no_backlog_row(
        loop_and_ctx_with_open_backlog, selected):
    """D18 是正 (段0 準致命): 空文字は選択なしとして扱う (fail closed —
    実在しない idea を勝者にしない)、というコード自身のコメントが明記
    する契約を pin する。`idea` キー欠落 / `""` / `"   "` の 3 値で撃つ
    (§6.6) — 非空値しか渡さないと `or ""` 型の退行が丸ごと残る。変異
    (`if not selected_idea.strip(): ... → if False: ...`) が入ると、
    空の idea が `improvement_backlog` へ INSERT され Tx-1 CAS で選択
    されて `run` に bind されてしまう。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    before = conn.execute(
        "SELECT count(*) c FROM improvement_backlog").fetchone()["c"]

    output = {"discoveries": [], "selected": selected,
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    outcome = loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))

    assert outcome.won is False
    assert outcome.backlog_id is None
    after = conn.execute(
        "SELECT count(*) c FROM improvement_backlog").fetchone()["c"]
    assert after == before
    run = conn.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                       (ctx.run_id,)).fetchone()
    assert run["backlog_id"] is None


def test_new_idea_selected_creates_and_binds_in_same_tx(loop_and_ctx_with_open_backlog):
    """新規 idea が selected の場合、INSERT → UPDATE(select) → bind が
    同じ tx で連鎖する。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    output = {"discoveries": [],
              "selected": {"backlog_id": None, "idea": "brand new idea"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    outcome = loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))
    assert outcome.won is True
    row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE idea='brand new idea'"
    ).fetchone()
    assert row["status"] == "selected"


def test_python_and_sql_idea_normalization_agree_on_internal_whitespace(
        loop_and_ctx_with_open_backlog):
    """M7: Python 側 `_norm` と SQL 側 `lower(trim(idea))` が内部空白処理で
    一致していることを verify する (内部空白を畳む vs 畳まない)。
    前後空白のみ除去する正規化で両者が一致。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog

    # 両側空白あり
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at) VALUES ('  with  spaces  ', 'agent', 'open', ?, ?)",
        (datetime(2026, 8, 1).isoformat(),) * 2)

    # 内部に連続空白
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at) VALUES ('internal   multiple', 'agent', 'open', ?, ?)",
        (datetime(2026, 8, 1).isoformat(),) * 2)
    conn.commit()

    # 同じテキストで discovery を追加（前後空白のみ異なる）
    output = {"discoveries": [
        {"idea": "with  spaces", "source": "agent", "evidence": "e"},
        {"idea": "internal   multiple", "source": "agent", "evidence": "e"}
    ],
              "selected": {"backlog_id": None, "idea": "x"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))

    # 前後空白のみ異なるものは重複として扱われ、追加されていない
    with_spaces_count = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "lower(trim(idea))='with  spaces'"  # 内部空白は畳まない
    ).fetchone()["c"]
    assert with_spaces_count == 1

    internal_multiple_count = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "lower(trim(idea))='internal   multiple'"
    ).fetchone()["c"]
    assert internal_multiple_count == 1

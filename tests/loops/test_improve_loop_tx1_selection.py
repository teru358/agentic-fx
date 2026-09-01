"""Tx-1: discoveries + backlog CAS + run bind (設計書 §4.1/§4.2-2、
プラン §8.1-24)。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

_NOW_ISO = datetime(2026, 8, 22, tzinfo=timezone.utc).isoformat()


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


@pytest.mark.parametrize("kind,expected_status", [("fact", "note"), ("task", "open")])
def test_discovery_kind_controls_initial_backlog_status(
        loop_and_ctx_with_open_backlog, kind, expected_status):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    output = {"discoveries": [{"idea": f"{kind} discovery", "source": "agent",
                                "evidence": "e", "kind": kind}],
              "selected": {"backlog_id": backlog_id, "idea": "x"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}

    loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))

    row = conn.execute("SELECT status FROM improvement_backlog WHERE idea=?",
                       (f"{kind} discovery",)).fetchone()
    assert row["status"] == expected_status


def test_internal_discovery_missing_kind_defaults_to_open(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    output = {"discoveries": [{"idea": "legacy task", "source": "agent",
                                "evidence": "e"}],
              "selected": {"backlog_id": backlog_id, "idea": "x"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))
    row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE idea='legacy task'").fetchone()
    assert row["status"] == "open"


def test_duplicate_idea_normalized_whitespace_and_case_is_deduped(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    # 大文字小文字が混在した既存 idea を作成
    # round2 #8 是正 (2026-08-29): 正規化は Python 側の idea_norm 列に
    # 一本化した (verified-round2.md #8)。本番の書き込み経路
    # (backlog.add / _select_and_bind の INSERT) はすべて idea_norm を
    # 書くため、ここも揃える (SQL 側 lower(trim(idea)) はもう重複検出に
    # 使われない)。
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at, idea_norm) VALUES ('RSI Divergence', 'agent', 'open', "
        "?, ?, 'rsi divergence')",
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
        "idea_norm='rsi divergence'").fetchone()["c"]
    assert n == 1  # 重複が追加されていない


def test_discovery_dedup_normalizes_unicode_whitespace_and_trailing_newline(
        loop_and_ctx_with_open_backlog):
    """round2 #8 是正 (2026-08-29、verified-round2.md #8): SQLite の
    lower(trim(idea)) は ASCII のみの trim/lower — `'improve X\\n'` は
    trim されず、`'IMPROVE Ä'` は非 ASCII 大文字が小文字化されない
    (probe 実測: `'improve X\\n'`→sql='improve x\\n' py='improve x'、
    `'IMPROVE Ä'`→sql='improve Ä' py='improve ä')。Python 側 `idea_norm`
    列への一本化でこの穴を塞ぐ。ASCII 空白だけの対では現行実装でも通って
    しまい変異を殺せないため、末尾改行と非 ASCII 大文字を使う。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog

    # Mission A: 末尾改行付きの discovery を投入
    output_a = {"discoveries": [{"idea": "improve X\n", "source": "agent",
                                 "evidence": "e"}],
               "selected": {"backlog_id": backlog_id, "idea": "x"},
               "artifact": {"type": "observation", "reason": "x"},
               "selection_rationale": "x"}
    loop._select_and_bind(conn, output_a, ctx, now=datetime(2026, 8, 22))

    # Mission B: 改行の無い同一 idea を投入 — 正規化後は重複
    output_b = {"discoveries": [{"idea": "improve X", "source": "agent",
                                 "evidence": "e"}],
               "selected": {"backlog_id": backlog_id, "idea": "x"},
               "artifact": {"type": "observation", "reason": "x"},
               "selection_rationale": "x"}
    loop._select_and_bind(conn, output_b, ctx, now=datetime(2026, 8, 22))

    n = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "idea_norm='improve x'").fetchone()["c"]
    assert n == 1

    # 非ASCII 大文字版も同じテスト内で確認する
    output_c = {"discoveries": [{"idea": "IMPROVE Ä", "source": "agent",
                                 "evidence": "e"}],
               "selected": {"backlog_id": backlog_id, "idea": "x"},
               "artifact": {"type": "observation", "reason": "x"},
               "selection_rationale": "x"}
    loop._select_and_bind(conn, output_c, ctx, now=datetime(2026, 8, 22))
    output_d = {"discoveries": [{"idea": "improve ä", "source": "agent",
                                 "evidence": "e"}],
               "selected": {"backlog_id": backlog_id, "idea": "x"},
               "artifact": {"type": "observation", "reason": "x"},
               "selection_rationale": "x"}
    loop._select_and_bind(conn, output_d, ctx, now=datetime(2026, 8, 22))

    n2 = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "idea_norm='improve ä'").fetchone()["c"]
    assert n2 == 1

    # selected.idea の binding も同じ正規形で当たること (backlog_id=None
    # のときに idea_norm で既存行を見つけて再利用する経路)。
    output_e = {"discoveries": [],
               "selected": {"backlog_id": None, "idea": "improve X\n"},
               "artifact": {"type": "observation", "reason": "x"},
               "selection_rationale": "x"}
    outcome = loop._select_and_bind(conn, output_e, ctx, now=datetime(2026, 8, 22))
    n3 = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "idea_norm='improve x'").fetchone()["c"]
    assert n3 == 1  # selected 側でも新規行を作らず既存行へ束ねる


def test_new_backlog_over_limit_are_dropped_and_counted_in_activity(
        loop_and_ctx_with_open_backlog, monkeypatch, tmp_path):
    """L-B30 是正 (束D検収, verified-local-round1.md §11 #21): テスト名は
    「activity に dropped 件数が記録されること」を謳うが、本体は挿入件数
    (`n == 2`) しか assert しておらず、テスト名が謳う activity の
    `dropped=3` も `inserted + dropped == len(discoveries)` も見ていなかった。
    両方を追加で assert する。"""
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
    assert n + 3 == len(discoveries)  # inserted + dropped == len(discoveries)
    activity_text = (tmp_path / "activity.log").read_text()
    assert f"mission={ctx.mission_id} dropped=3" in activity_text


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


# round2 最終是正 A8 (2026-08-29、verified-local-round2.md A8):
# `"idea_norm=? AND status IN ('open','observation')"` フィルタが未 pin
# だった (`status IN (...)` を落とす変異が生存)。`status='done'` の同一
# idea 行を置いた状態で新規 idea を selected に流し、既存 done 行を巻き
# 戻さずに新規行が作られて won=True になることを固定する。
def test_new_idea_selected_ignores_done_row_with_same_idea_norm(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at, idea_norm) VALUES (?,?,'done',?,?,?)",
        ("brand new idea", "user", _NOW_ISO, _NOW_ISO, "brand new idea"))
    conn.commit()
    done_row = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea='brand new idea' "
        "AND status='done'").fetchone()

    output = {"discoveries": [],
              "selected": {"backlog_id": None, "idea": "brand new idea"},
              "artifact": {"type": "observation", "reason": "x"},
              "selection_rationale": "x"}
    outcome = loop._select_and_bind(conn, output, ctx, now=datetime(2026, 8, 22))

    assert outcome.won is True
    rows = conn.execute(
        "SELECT id, status FROM improvement_backlog WHERE idea='brand new idea'"
    ).fetchall()
    assert len(rows) == 2  # 既存 done 行 + 新規 selected 行
    by_id = {r["id"]: r["status"] for r in rows}
    assert by_id[done_row["id"]] == "done"  # 既存 done 行は触られない
    new_row_id = next(rid for rid in by_id if rid != done_row["id"])
    assert by_id[new_row_id] == "selected"


def test_python_and_sql_idea_normalization_agree_on_internal_whitespace(
        loop_and_ctx_with_open_backlog):
    """M7: 内部空白を畳まない正規化 (前後空白のみ除去) の pin。

    round2 #8 是正 (2026-08-29): 正規化は Python 側の `idea_norm` 列に
    一本化した (SQL の lower(trim(idea)) はもう重複検出に使われない —
    verified-round2.md #8、SQLite の lower()/trim() は ASCII のみで
    Python の str.strip().lower() と非 ASCII 空白/大文字で食い違うため)。
    このテスト自体の主張 (前後空白のみ除去・内部空白は畳まない) は
    Python 側 `_norm` 一本になっても変わらないので、fixture 行にも
    本番の書き込み経路と同じ `idea_norm` を持たせて揃える。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog

    # 両側空白あり
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at, idea_norm) VALUES ('  with  spaces  ', 'agent', 'open', "
        "?, ?, 'with  spaces')",
        (datetime(2026, 8, 1).isoformat(),) * 2)

    # 内部に連続空白
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at, idea_norm) VALUES ('internal   multiple', 'agent', "
        "'open', ?, ?, 'internal   multiple')",
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
        "idea_norm='with  spaces'"  # 内部空白は畳まない
    ).fetchone()["c"]
    assert with_spaces_count == 1

    internal_multiple_count = conn.execute(
        "SELECT count(*) c FROM improvement_backlog WHERE "
        "idea_norm='internal   multiple'"
    ).fetchone()["c"]
    assert internal_multiple_count == 1

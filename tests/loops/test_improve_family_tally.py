"""終端時の申し送り (`family_tally`) は、族別試行結果と同じ 1 件ずつの投影を通る。

同じ DB 状態・同じ配備の集合・同じ許可 pair を入力すれば、族別試行結果の同じ
表示 key の行と同じ値になる。"""
from __future__ import annotations

from agentic_fx.loops import improve_context
from agentic_fx.loops.improve_context import (
    UNKNOWN_FAMILY_LABEL, deployed_families, family_tally)

from tests.loops.test_improve_family_attempts import (  # noqa: F401
    _meta, _plant, _rows, _section, conn)

ALLOWED = {"USDJPY"}


def _tally(conn, metas, key, allowed=ALLOWED):
    return family_tally(conn, allowed_pairs=set(allowed),
                        families=deployed_families(list(metas)), family_key=key)


def _row_for(conn, tmp_path, metas, label):
    return _rows(_section(conn, tmp_path, metas=metas))[label]


def test_tally_matches_the_family_attempts_row_of_the_same_display_key(
        conn, tmp_path):
    metas = [_meta("rsi", "indicator"),
             _meta("rsi_cross", "strategy", indicators=("rsi",))]
    key = "strategy:rsi@1h@USDJPY"
    for outcome in ("not_submitted", "unprofitable", "duplicate_logic",
                    "pending", "gate_failed", "approved"):
        _plant(conn, key, outcome=outcome)
    _plant(conn, "strategy:other_ind@1h@USDJPY", outcome="pending")
    tally = _tally(conn, metas, key)
    row = _row_for(conn, tmp_path, metas, tally["family_key"])
    assert tally["family_key"] == "strategy:rsi@1h@USDJPY"
    assert (tally["attempts"], tally["outcomes"]) == (
        row["attempts"], row["outcomes"])
    assert tally["outcomes"] == {"not_submitted": 3, "duplicate": 1,
                                 "pending": 1, "approved": 1}


def test_raw_keys_that_differ_only_in_undeployed_indicators_merge(
        conn, tmp_path):
    metas = [_meta("rsi", "indicator")]
    _plant(conn, "strategy:foo,rsi@1h@USDJPY")
    _plant(conn, "strategy:bar,rsi@1h@USDJPY", outcome="pending")
    tally = _tally(conn, metas, "strategy:foo,rsi@1h@USDJPY")
    assert tally["family_key"] == "strategy:rsi,未配備指標@1h@USDJPY"
    assert tally["attempts"] == 2
    row = _row_for(conn, tmp_path, metas, tally["family_key"])
    assert (tally["attempts"], tally["outcomes"]) == (
        row["attempts"], row["outcomes"])
    assert tally["outcomes"] == {"not_submitted": 1, "pending": 1}


def test_internal_outcomes_are_folded_into_the_public_words(conn):
    metas = [_meta("rsi", "indicator")]
    key = "indicator:rsi@1h"
    for outcome in ("unprofitable", "insufficient_trades", "gate_failed",
                    "noop_copy", "family_redirect", "duplicate_metrics"):
        _plant(conn, key, outcome=outcome)
    _plant(conn, key, outcome="made_up_value")
    tally = _tally(conn, metas, key)
    assert tally["outcomes"] == {"not_submitted": 5, "duplicate": 1,
                                 "unclassified": 1}
    assert set(tally["outcomes"]) <= set(improve_context.PUBLIC_OUTCOMES)


def test_other_families_are_not_counted(conn):
    metas = [_meta("rsi", "indicator"), _meta("atr", "indicator")]
    _plant(conn, "indicator:rsi@1h")
    _plant(conn, "indicator:atr@1h")
    _plant(conn, "indicator:atr@1h")
    assert _tally(conn, metas, "indicator:rsi@1h")["attempts"] == 1
    assert _tally(conn, metas, "indicator:atr@1h")["attempts"] == 2


def test_an_unreadable_key_counts_under_the_unknown_family(conn, tmp_path):
    metas = [_meta("rsi", "indicator")]
    _plant(conn, "totally broken key")
    _plant(conn, "strategy:rsi@1h@EURUSD")  # 許可 pair 外は読めない
    tally = _tally(conn, metas, "totally broken key")
    assert tally["family_key"] == UNKNOWN_FAMILY_LABEL
    row = _row_for(conn, tmp_path, metas, UNKNOWN_FAMILY_LABEL)
    assert (tally["attempts"], tally["outcomes"]) == (
        row["attempts"], row["outcomes"])


def test_same_inputs_give_the_same_value_even_when_the_prompt_omits_the_row(
        conn, tmp_path):
    """表示件数の打ち切りは掛けない: 終端時点の集計は台帳の行単位。"""
    metas = [_meta("rsi", "indicator")]
    for n in range(80):
        _plant(conn, f"indicator:ind{n}@1h")
    key = "indicator:rsi@1h"
    _plant(conn, key, outcome="pending")
    assert _tally(conn, metas, key)["attempts"] == 1


def test_a_changed_deployment_changes_the_display_key(conn):
    key = "strategy:rsi@1h@USDJPY"
    _plant(conn, key)
    deployed = _tally(conn, [_meta("rsi", "indicator")], key)
    undeployed = _tally(conn, [], key)
    assert deployed["family_key"] == "strategy:rsi@1h@USDJPY"
    assert undeployed["family_key"] == "strategy:未配備指標@1h@USDJPY"


def test_the_prompt_and_the_tally_share_one_projection(conn, tmp_path,
                                                       monkeypatch):
    seen: list[str] = []
    original = improve_context.project_attempt

    def spy(record, **kwargs):
        seen.append(record["family_key"])
        return original(record, **kwargs)

    _plant(conn, "indicator:rsi@1h")
    metas = [_meta("rsi", "indicator")]
    with monkeypatch.context() as mp:
        mp.setattr(improve_context, "project_attempt", spy)
        _section(conn, tmp_path, metas=metas)
        _tally(conn, metas, "indicator:rsi@1h")
    assert seen == ["indicator:rsi@1h", "indicator:rsi@1h"]


def test_family_attempts_output_is_unchanged_by_the_extraction(conn, tmp_path):
    """1 件ずつの投影を切り出す前後で、prompt に入る表が同じ文字列になる。"""
    metas = [_meta("rsi", "indicator"),
             _meta("rsi_cross", "strategy", indicators=("rsi",))]
    _plant(conn, "indicator:rsi@1h", outcome="approved",
           created="2026-10-05T01:02:03+00:00")
    _plant(conn, "strategy:rsi@1h@USDJPY", outcome="unprofitable",
           created="2026-10-06T01:02:03+00:00")
    _plant(conn, "strategy:rsi@1h@USDJPY", outcome="duplicate_logic",
           created="2026-10-07T01:02:03+00:00")
    _plant(conn, "strategy:foo,rsi@1h@USDJPY", outcome="pending",
           created="2026-09-07T01:02:03+00:00")
    _plant(conn, "indicator:zzz@4h", outcome="gate_failed",
           created="2026-08-07T01:02:03+00:00")
    _plant(conn, "garbage", outcome="expired",
           created="2026-08-09T01:02:03+00:00")
    text = improve_context.render_family_attempts(
        _section(conn, tmp_path, metas=metas))
    assert text == EXPECTED_TABLE


EXPECTED_TABLE = (
    "| family_key | attempts | outcomes | last_attempt_month | deployed |\n"
    "|---|---:|---|---|---|\n"
    "| indicator:rsi@1h | 1 | approved=1 | 2026-10 | yes (rsi) |\n"
    "| strategy:rsi@1h@USDJPY | 2 | not_submitted=1, duplicate=1 | 2026-10 | yes (rsi_cross) |\n"
    "\n"
    "| family_key | attempts | outcomes | last_attempt_month | deployed |\n"
    "|---|---:|---|---|---|\n"
    "| strategy:rsi,未配備指標@1h@USDJPY | 1 | pending=1 | 2026-09 | no |\n"
    "| indicator (未配備) @4h | 1 | not_submitted=1 | 2026-08 | no |\n"
    "| 族不明 | 1 | expired=1 | 2026-08 | no |")

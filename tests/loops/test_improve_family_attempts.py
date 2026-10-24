"""仮説族ごとの試行状況 (`improvement_history.family_attempts`) と prompt の節。

archive の 3 列だけを親が集計し、配備済み族と 1 行に統合して表示する。
結果は固定語彙、識別子は文法と再生成照合を通ったものだけ、時刻は年月だけ。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.loops import improve_context
from agentic_fx.loops.improve_context import (
    FAMILY_SECTION_MAX_BYTES, FAMILY_SECTION_MAX_ENTRIES, PUBLIC_OUTCOMES,
    UNKNOWN_FAMILY_LABEL, build_improve_context, project_outcome,
    render_family_attempts)
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.loader import IndicatorRef, PluginMeta
from agentic_fx.plugin.logic_identity import parse_family_key
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import candidate_archives as archives
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
_EXAMPLE = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _settings(pairs=("USDJPY",)):
    return _EXAMPLE.model_copy(update={"pairs": list(pairs)})


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "fa.db")
    init_db(c)
    yield c
    c.close()


_serial = count(1)


def _mission(conn) -> int:
    return missions_store.start(conn, "improve", "codex", "gpt-5", now=NOW,
                                commit=True)


def _plant(conn, key, *, outcome="not_submitted",
           created="2026-10-05T01:02:03+00:00", mission_id=None,
           metrics=None) -> int:
    """台帳へ 1 行置く。列の値は直接書き、集合外の outcome も置ける。"""
    if mission_id is None:
        mission_id = _mission(conn)
    n = next(_serial)
    row_id = archives.insert(
        conn, mission_id=mission_id, name=f"cand{n}",
        content_hash=f"content{n}", artifact_hash=f"artifact{n}",
        archive_path=None, pair="USDJPY", metrics=metrics or {"pf": 0.5},
        now=NOW, family_key=key, commit=True)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute(
        "UPDATE candidate_archives SET family_key=?, outcome_class=?, "
        "created_at=? WHERE id=?", (key, outcome, created, row_id))
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints=OFF")
    return row_id


def _meta(name, kind, *, timeframe="1h", pairs=("USDJPY",), indicators=()):
    return PluginMeta(
        name=name, kind=kind, path=Path("."), params={"secret_param": 7},
        timeframe=timeframe, pairs=tuple(pairs), max_bars=100,
        content_hash="c" * 64, artifact_hash="a" * 64,
        indicators=tuple(IndicatorRef(alias=p, plugin=p, params={}, pin=None)
                         for p in indicators),
        outputs=("v",) if kind == "indicator" else None)


def _context(conn, tmp_path, *, settings=None, metas=()):
    return build_improve_context(
        conn, settings=settings or _settings(), now=NOW, root=tmp_path,
        allowed_backlog_ids=None, inventory_metas=list(metas))


def _section(conn, tmp_path, **kwargs):
    return _context(conn, tmp_path, **kwargs)[
        "improvement_history"]["family_attempts"]


class _Ctx:
    def __init__(self, tmp_path):
        self.staging_dir = tmp_path / "staging"
        self.source_snapshot_dir = tmp_path / "source"
        self.inventory_view = {"plugins": [], "pin_broken_strategies": []}


def _prompt(conn, tmp_path, *, settings=None, metas=()):
    settings = settings or _settings()
    ctx_data = build_improve_context(
        conn, settings=settings, now=NOW, root=tmp_path,
        allowed_backlog_ids=None,
        inventory_view={"plugins": [], "pin_broken_strategies": []},
        inventory_metas=list(metas))
    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = settings
    return loop._render_improve_mission_prompt(ctx_data, ctx=_Ctx(tmp_path))


def _rows(section):
    return {r["family_key"]: r for r in section["rows"]}


# --- AC-1: 読取面は 3 列だけ -----------------------------------------------

def test_ac1_projection_returns_exactly_three_columns(conn):
    _plant(conn, "indicator:rsi@1h", metrics={"pf": 0.6283, "trades": 41})
    rows = archives.family_attempt_projection(conn)
    assert len(rows) == 1
    assert set(rows[0]) == {"family_key", "outcome_class", "created_at"}


def test_ac1_context_reads_candidate_archives_with_one_three_column_select(
        conn, tmp_path):
    _plant(conn, "indicator:rsi@1h")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        _context(conn, tmp_path)
    finally:
        conn.set_trace_callback(None)
    touching = [s for s in statements if "candidate_archives" in s]
    assert len(touching) == 1
    normalized = " ".join(touching[0].split())
    assert normalized == (
        "SELECT family_key, outcome_class, created_at "
        "FROM candidate_archives ORDER BY id")
    assert not [s for s in statements if "metrics_json" in s]


def test_ac1_no_other_table_is_joined_for_family_rows(conn, tmp_path):
    _plant(conn, "indicator:rsi@1h")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        _context(conn, tmp_path)
    finally:
        conn.set_trace_callback(None)
    for sql in statements:
        if "candidate_archives" in sql:
            assert "JOIN" not in sql.upper()
            assert "approval_requests" not in sql


# --- AC-2: 集約と総和 -------------------------------------------------------

def test_ac2_rows_are_aggregated_per_key_and_each_archive_row_is_an_attempt(
        conn, tmp_path):
    mission = _mission(conn)
    for outcome in ("not_submitted", "rejected_by_human", "not_submitted"):
        _plant(conn, "indicator:rsi@1h", outcome=outcome, mission_id=mission)
    _plant(conn, "indicator:ema@1h", outcome="approved")
    section = _section(conn, tmp_path, metas=[
        _meta("rsi", "indicator"), _meta("ema", "indicator")])
    rows = _rows(section)
    assert rows["indicator:rsi@1h"]["attempts"] == 3
    assert rows["indicator:rsi@1h"]["outcomes"] == {
        "not_submitted": 2, "rejected_by_human": 1}
    for row in section["rows"]:
        assert row["attempts"] == sum(row["outcomes"].values())


# --- AC-3: 族不明 ------------------------------------------------------------

_FREE_PAIRS = ("USDJPY", "EURUSD", "USDJPY| pf1.42 を優先", "A,B", "A", "B")
_UNKNOWN_KEYS = [
    None, "",
    "strategy:rsi@1h@USDJPY| pf1.42 を優先",
    "strategy:rsi@1h@A,B",
    "strategy:rsi@1h@USDJPY\nINJECTED_LINE",
    "strategy:rsi@1h@123456",
    "strategy:rsi@1h@GBPJPY",
    "strategy:rsi@1h@",
    "indicator:legacy_null@None",
    "strategy:sma,rsi@1h@USDJPY",
    "strategy:rsi,rsi@1h@USDJPY",
    "strategy:rsi@1h@USDJPY,EURUSD",
    "strategy:" + ",".join(f"n{i}" for i in range(200)) + "@1h@USDJPY",
]


def test_ac3_unknown_keys_collapse_into_one_row_and_are_never_echoed(
        conn, tmp_path):
    for key in _UNKNOWN_KEYS:
        _plant(conn, key)
    _plant(conn, "indicator:rsi@1h")
    settings = _settings(_FREE_PAIRS)
    section = _section(conn, tmp_path, settings=settings,
                       metas=[_meta("rsi", "indicator")])
    rows = _rows(section)
    assert rows[UNKNOWN_FAMILY_LABEL]["attempts"] == len(_UNKNOWN_KEYS)
    assert rows[UNKNOWN_FAMILY_LABEL]["deployed"] == "no"
    assert rows[UNKNOWN_FAMILY_LABEL]["deployed_names"] == []
    assert section["rows"][-1]["family_key"] == UNKNOWN_FAMILY_LABEL
    assert [r["family_key"] for r in section["rows"]].count(
        UNKNOWN_FAMILY_LABEL) == 1
    text = _prompt(conn, tmp_path, settings=settings,
                   metas=[_meta("rsi", "indicator")])
    blob = text + json.dumps(section, ensure_ascii=False)
    for needle in ("pf1.42", "を優先", "INJECTED_LINE", "123456", "GBPJPY",
                   "@None", "n199", "strategy:sma,rsi", "strategy:rsi,rsi"):
        assert needle not in blob, needle


# --- AC-4 / AC-15: 結果区分 ---------------------------------------------------

_EXPECTED = {
    "not_submitted": "not_submitted", "unprofitable": "not_submitted",
    "insufficient_trades": "not_submitted", "gate_failed": "not_submitted",
    "noop_copy": "not_submitted", "family_redirect": "not_submitted",
    "duplicate_logic": "duplicate", "duplicate_metrics": "duplicate",
    "pending": "pending", "approved": "approved",
    "rejected_by_human": "rejected_by_human", "expired": "expired",
    "invalidated": "invalidated", "revoked": "revoked",
}


@pytest.mark.parametrize("stored,public", sorted(_EXPECTED.items()))
def test_ac4_stored_outcomes_project_to_the_public_vocabulary(stored, public):
    assert project_outcome(stored) == public


@pytest.mark.parametrize("raw", [
    "something_new", "", "UNPROFITABLE", "not_submitted ", None, 7, b"x",
    "unclassified"])
def test_ac4_unknown_outcomes_are_unclassified(raw):
    assert project_outcome(raw) == "unclassified"


def test_ac4_public_label_set_is_exactly_the_nine_words(conn, tmp_path):
    for stored in list(_EXPECTED) + ["weird_value", ""]:
        _plant(conn, "indicator:rsi@1h", outcome=stored)
    section = _section(conn, tmp_path, metas=[_meta("rsi", "indicator")])
    seen = set()
    for row in section["rows"]:
        seen.update(row["outcomes"])
    assert seen == set(PUBLIC_OUTCOMES)
    assert len(PUBLIC_OUTCOMES) == 9
    text = render_family_attempts(section)
    for raw in ("unprofitable", "insufficient_trades", "gate_failed",
                "noop_copy", "family_redirect", "duplicate_logic",
                "duplicate_metrics", "weird_value"):
        assert raw not in text, raw
    row = _rows(section)["indicator:rsi@1h"]
    assert row["outcomes"]["unclassified"] == 2
    assert row["outcomes"]["not_submitted"] == 6
    assert row["outcomes"]["duplicate"] == 2


def test_ac15_three_internal_states_look_the_same_in_a_family_row(
        conn, tmp_path):
    for stored in ("unprofitable", "insufficient_trades", "gate_failed"):
        _plant(conn, "indicator:rsi@1h", outcome=stored)
    section = _section(conn, tmp_path, metas=[_meta("rsi", "indicator")])
    row = _rows(section)["indicator:rsi@1h"]
    assert row["outcomes"] == {"not_submitted": 3}
    assert "not_submitted=3 |" in render_family_attempts(section)


# --- AC-5: 配備済み族との統合 -----------------------------------------------------

def test_ac5_deployed_families_merge_with_archive_rows(conn, tmp_path):
    metas = [
        _meta("rsi_ind", "indicator"),
        _meta("ema_ind", "indicator", timeframe="4h"),
        _meta("rsi_strat", "strategy", indicators=("rsi_ind",)),
        _meta("bare_strat", "strategy"),
        _meta("sig_x", "signal", pairs=("USDJPY",)),
    ]
    _plant(conn, "strategy:rsi_ind@1h@USDJPY", outcome="family_redirect")
    _plant(conn, "strategy:rsi_ind@1h@USDJPY", outcome="not_submitted")
    section = _section(conn, tmp_path, metas=metas)
    rows = _rows(section)
    merged = rows["strategy:rsi_ind@1h@USDJPY"]
    assert merged["deployed"] == "yes"
    assert merged["deployed_names"] == ["rsi_strat"]
    assert merged["outcomes"] == {"not_submitted": 2}
    untried = rows["indicator:ema_ind@4h"]
    assert untried["attempts"] == 0 and untried["outcomes"] == {}
    assert untried["last_attempt_month"] is None
    kinds = {u["name"]: u["kind"] for u in section["unkeyed_deployed"]}
    assert kinds == {"bare_strat": "strategy", "sig_x": "signal"}
    assert UNKNOWN_FAMILY_LABEL not in rows
    text = _prompt(conn, tmp_path, metas=metas)
    assert text.count("bare_strat") == 1
    assert text.count("sig_x") == 1
    assert text.count("rsi_strat") == 1
    assert "## 配備済みの仮説族" not in text
    assert "secret_param" not in text


def test_ac5_unkeyed_deployed_keeps_its_composition(conn, tmp_path):
    metas = [_meta("bare_strat", "strategy", timeframe="4h",
                   pairs=("USDJPY", "EURUSD"))]
    section = _section(conn, tmp_path, metas=metas)
    assert section["unkeyed_deployed"] == [{
        "name": "bare_strat", "kind": "strategy", "timeframe": "4h",
        "pairs": ["USDJPY", "不明"], "indicators": []}]
    text = render_family_attempts(section)
    assert ("- bare_strat (strategy): timeframe=4h, pairs=USDJPY, 不明, "
            "indicators=[(なし)]") in text


def test_ac5_deployed_plugin_with_a_bad_pair_has_no_key(conn, tmp_path):
    metas = [_meta("odd_strat", "strategy", indicators=("rsi_ind",),
                   pairs=("EURUSD,USDJPY",)),
             _meta("rsi_ind", "indicator")]
    settings = _settings(("USDJPY", "EURUSD,USDJPY"))
    section = _section(conn, tmp_path, settings=settings, metas=metas)
    assert [u["name"] for u in section["unkeyed_deployed"]] == ["odd_strat"]
    assert section["unkeyed_deployed"][0]["pairs"] == ["不明"]
    assert "strategy:rsi_ind@1h@EURUSD,USDJPY" not in json.dumps(
        section, ensure_ascii=False)


# --- AC-6: 順序と上限 -----------------------------------------------------------

def _many_pairs(n):
    return [f"{chr(65 + i // 26)}{chr(65 + i % 26)}CDEF" for i in range(n)]


def test_ac6_order_is_deployed_then_unkeyed_then_undeployed_then_unknown(
        conn, tmp_path):
    pairs = _many_pairs(6)
    metas = [_meta("rsi_ind", "indicator"), _meta("bare", "strategy",
                                                   pairs=("USDJPY",)),
             _meta("rsi_strat", "strategy", indicators=("rsi_ind",),
                   pairs=(pairs[0],))]
    settings = _settings(["USDJPY", *pairs])
    plan = [  # (pair, attempts, month)
        (pairs[1], 1, "2026-09"), (pairs[2], 2, "2026-03"),
        (pairs[3], 2, "2026-08"), (pairs[4], 2, "2026-08"),
        (pairs[5], 5, "2026-01")]
    for pair, attempts, month in plan:
        for _ in range(attempts):
            _plant(conn, f"strategy:rsi_ind@1h@{pair}",
                   created=f"{month}-10T00:00:00+00:00")
    _plant(conn, None)
    section = _section(conn, tmp_path, settings=settings, metas=metas)
    keys = [r["family_key"] for r in section["rows"]]
    expected_deployed = ["indicator:rsi_ind@1h",
                         f"strategy:rsi_ind@1h@{pairs[0]}"]
    assert keys[:2] == sorted(expected_deployed)
    assert keys[2:] == [
        f"strategy:rsi_ind@1h@{pairs[5]}",     # attempts 5
        f"strategy:rsi_ind@1h@{pairs[3]}",     # attempts 2, 2026-08, key 昇順
        f"strategy:rsi_ind@1h@{pairs[4]}",
        f"strategy:rsi_ind@1h@{pairs[2]}",     # attempts 2, 2026-03
        f"strategy:rsi_ind@1h@{pairs[1]}",     # attempts 1
        UNKNOWN_FAMILY_LABEL]
    assert [u["name"] for u in section["unkeyed_deployed"]] == ["bare"]
    text = render_family_attempts(section)
    unkeyed_at = text.index("family key なしの配備済み plugin")
    assert unkeyed_at > text.index("| indicator:rsi_ind@1h |")
    assert unkeyed_at > text.index(f"| strategy:rsi_ind@1h@{pairs[0]} |")
    assert unkeyed_at < text.index(f"| strategy:rsi_ind@1h@{pairs[5]} |")
    assert unkeyed_at < text.index(UNKNOWN_FAMILY_LABEL)


def test_ac6_rendered_order_deployed_unkeyed_undeployed_unknown(
        conn, tmp_path):
    metas = [_meta("rsi", "indicator"),
             _meta("bare", "strategy", pairs=("USDJPY",))]
    _plant(conn, "indicator:ema@1h")
    _plant(conn, None)
    section = _section(conn, tmp_path, metas=metas)
    text = render_family_attempts(section)
    positions = [text.index(marker) for marker in (
        "| indicator:rsi@1h |", "- bare (strategy)",
        "| indicator (未配備) @1h |", f"| {UNKNOWN_FAMILY_LABEL} |")]
    assert positions == sorted(positions)
    prompt = _prompt(conn, tmp_path, metas=metas)
    prompt_positions = [prompt.index(marker) for marker in (
        "| indicator:rsi@1h |", "- bare (strategy)",
        "| indicator (未配備) @1h |", f"| {UNKNOWN_FAMILY_LABEL} |")]
    assert prompt_positions == sorted(prompt_positions)


def test_ac6_entry_limit_keeps_deployed_first_and_reserves_unknown(
        conn, tmp_path):
    pairs = _many_pairs(70)
    metas = [_meta("rsi_ind", "indicator")]
    settings = _settings(["USDJPY", *pairs])
    for pair in pairs:
        _plant(conn, f"strategy:rsi_ind@1h@{pair}")
    _plant(conn, None)
    section = _section(conn, tmp_path, settings=settings, metas=metas)
    assert len(section["rows"]) == FAMILY_SECTION_MAX_ENTRIES == 50
    assert section["rows"][-1]["family_key"] == UNKNOWN_FAMILY_LABEL
    assert section["rows"][0]["family_key"] == "indicator:rsi_ind@1h"
    # 表示 51 + 省略 N = 全 entry (配備済み 1 + 非配備 70 + 族不明 1)
    assert section["entries_omitted"] == 72 - 50
    assert "他 22 件を省略" in render_family_attempts(section)


def test_ac6_byte_limit_drops_whole_rows_in_order(conn, tmp_path, monkeypatch):
    pairs = _many_pairs(130)
    base = pairs[:38]
    extras = pairs[38:98]
    settings = _settings(["USDJPY", *pairs])
    for extra in extras:
        key = "strategy:rsi_ind@1h@" + ",".join(sorted([*base, extra]))
        assert len(key.encode()) <= 512
        _plant(conn, key)
    metas = [_meta("rsi_ind", "indicator")]
    section = _section(conn, tmp_path, settings=settings, metas=metas)
    text = render_family_attempts(section)
    assert len(text.encode("utf-8")) <= FAMILY_SECTION_MAX_BYTES
    prompt = _prompt(conn, tmp_path, settings=settings, metas=metas)
    start = prompt.index("## 仮説族の試行状況")
    end = prompt.index("\n## ", start + 1)
    whole_section = prompt[start:end]
    assert text in whole_section
    assert len(whole_section.encode("utf-8")) <= FAMILY_SECTION_MAX_BYTES
    assert len(whole_section.encode("utf-8")) > FAMILY_SECTION_MAX_BYTES - 1024 - 2048
    assert 0 < len(section["rows"]) < FAMILY_SECTION_MAX_ENTRIES
    for line in text.splitlines():
        if line.startswith("| strategy:"):
            assert line.endswith(" | no |")
    total = 1 + len(extras)
    assert section["entries_omitted"] == total - len(section["rows"])
    monkeypatch.setattr(improve_context, "FAMILY_SECTION_MAX_BYTES", 10 ** 9)
    monkeypatch.setattr(improve_context, "FAMILY_SECTION_MAX_ENTRIES", 10 ** 6)
    full = _section(conn, tmp_path, settings=settings, metas=metas)
    assert len(full["rows"]) == total
    assert full["rows"][:len(section["rows"])] == section["rows"]


# --- AC-7: 表の形、月、deployed 列 -----------------------------------------------

def test_ac7_table_has_five_columns_and_fixed_outcome_order(conn, tmp_path):
    _plant(conn, "indicator:rsi@1h", outcome="rejected_by_human")
    _plant(conn, "indicator:rsi@1h", outcome="not_submitted")
    _plant(conn, "indicator:rsi@1h", outcome="revoked")
    _plant(conn, "indicator:rsi@1h", outcome="approved")
    _plant(conn, "indicator:rsi@1h", outcome="duplicate_logic")
    text = render_family_attempts(_section(conn, tmp_path, metas=[
        _meta("rsi", "indicator")]))
    lines = text.splitlines()
    assert lines[0] == ("| family_key | attempts | outcomes | "
                        "last_attempt_month | deployed |")
    assert lines[1] == "|---|---:|---|---|---|"
    assert lines[2] == (
        "| indicator:rsi@1h | 5 | not_submitted=1, duplicate=1, approved=1, "
        "rejected_by_human=1, revoked=1 | 2026-10 | yes (rsi) |")


def test_ac7_month_is_utc_year_month_and_bad_dates_are_dropped(conn, tmp_path):
    _plant(conn, "indicator:a@1h", created="2026-10-31T23:30:00-05:00")
    _plant(conn, "indicator:b@1h", created="2026-03-04T05:06:07")
    _plant(conn, "indicator:c@1h", created="garbage-time-9f3e")
    _plant(conn, "indicator:c@1h", created="2025-12-31T23:59:59+00:00")
    _plant(conn, "indicator:d@1h", created="not a date")
    metas = [_meta(n, "indicator") for n in "abcd"]
    section = _section(conn, tmp_path, metas=metas)
    rows = _rows(section)
    assert rows["indicator:a@1h"]["last_attempt_month"] == "2026-11"
    assert rows["indicator:b@1h"]["last_attempt_month"] == "2026-03"
    assert rows["indicator:c@1h"]["last_attempt_month"] == "2025-12"
    assert rows["indicator:c@1h"]["attempts"] == 2
    assert rows["indicator:d@1h"]["last_attempt_month"] is None
    assert "| indicator:d@1h | 1 | not_submitted=1 | - | yes (d) |" in (
        render_family_attempts(section))
    assert "garbage-time" not in render_family_attempts(section)


def test_ac7_last_attempt_month_is_the_latest_valid_month(conn, tmp_path):
    for created in ("2026-04-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00",
                    "2026-06-15T00:00:00+00:00", "bad"):
        _plant(conn, "indicator:rsi@1h", created=created)
    row = _rows(_section(conn, tmp_path, metas=[_meta("rsi", "indicator")]))[
        "indicator:rsi@1h"]
    assert row["last_attempt_month"] == "2026-09"
    assert row["attempts"] == 4


def test_ac7_deployed_column_lists_five_names_then_the_rest(conn, tmp_path):
    names = [f"strat_{c}" for c in "gfedcba"]
    metas = [_meta("rsi_ind", "indicator")] + [
        _meta(n, "strategy", indicators=("rsi_ind",)) for n in names]
    text = render_family_attempts(_section(conn, tmp_path, metas=metas))
    assert ("| strategy:rsi_ind@1h@USDJPY | 0 | - | - | yes (strat_a, "
            "strat_b, strat_c, strat_d, strat_e, 他 2) |") in text


# --- AC-8: 成績 canary ------------------------------------------------------------

def test_ac8_metric_canaries_never_reach_context_or_prompt(conn, tmp_path):
    canary = {"pf": 0.62831853, "avg_r": 0.0771828, "trades": 424243,
              "win_rate": 0.5551213}
    row_id = _plant(conn, "indicator:rsi@1h", metrics=canary)
    stored = conn.execute("SELECT metrics_json FROM candidate_archives "
                          "WHERE id=?", (row_id,)).fetchone()[0]
    for value in canary.values():
        assert str(value) in stored
    metas = [_meta("rsi", "indicator")]
    text = _prompt(conn, tmp_path, metas=metas)
    blob = json.dumps(_context(conn, tmp_path, metas=metas), default=str,
                      ensure_ascii=False)
    assert "| indicator:rsi@1h | 1 |" in text
    for value in canary.values():
        assert str(value) not in text and str(value) not in blob
    assert "metrics" not in json.dumps(_section(conn, tmp_path, metas=metas))


def test_ac8_family_rows_have_exactly_the_public_keys(conn, tmp_path):
    _plant(conn, "indicator:rsi@1h")
    section = _section(conn, tmp_path, metas=[_meta("rsi", "indicator")])
    assert set(section) == {"rows", "unkeyed_deployed", "entries_omitted"}
    for row in section["rows"]:
        assert set(row) == {"family_key", "attempts", "outcomes",
                            "last_attempt_month", "deployed",
                            "deployed_names"}


# --- AC-9: 人間の reject 理由 -----------------------------------------------------

def test_ac9_reject_reason_stays_out_of_the_family_section(conn, tmp_path):
    from agentic_fx.store import approvals
    reason = "reasoncanary_family_55aa"
    backlog_id = backlog_store.add(conn, "idea", "user", NOW)
    backlog_store.select_for_mission(conn, backlog_id, now=NOW)
    improve_runs_store.start(conn, backlog_id, NOW)
    approval_id = approvals.create(conn, kind="plugin", now=NOW, payload={
        "backlog_id": backlog_id, "name": "cand_x"})
    approvals.apply_decision(conn, approval_id, "rejected",
                             decided_by="shell", now=NOW, reason=reason)
    _plant(conn, "indicator:rsi@1h", outcome="rejected_by_human")
    assert conn.execute("SELECT 1 FROM approval_requests WHERE reason=?",
                        (reason,)).fetchone() is not None
    metas = [_meta("rsi", "indicator")]
    text = _prompt(conn, tmp_path, metas=metas)
    assert "rejected_by_human=1" in text
    assert reason not in text
    assert reason not in json.dumps(_context(conn, tmp_path, metas=metas),
                                    default=str)


# --- AC-16: 候補名の投影 ----------------------------------------------------------

_LABEL_FORMS = (
    re.compile(r"indicator \(未配備\) @(15m|1h|4h|1d)"),
    re.compile(r"strategy:([a-z][a-z0-9_]*,)*未配備指標@(15m|1h|4h|1d)"
               r"@[A-Z]{6}(,[A-Z]{6})*"),
    re.compile(r"族不明"),
)


def _is_family_label(label, pairs):
    if parse_family_key(label, allowed_pairs=pairs) is not None:
        return True
    return any(form.fullmatch(label) for form in _LABEL_FORMS)


def test_ac16_undeployed_names_are_replaced_and_merged(conn, tmp_path):
    names = ("aa_undeployed_a", "aa_undeployed_b", "aa_undeployed_c")
    _plant(conn, f"indicator:{names[0]}@1h")
    _plant(conn, f"indicator:{names[1]}@1h", outcome="duplicate_metrics")
    _plant(conn, f"indicator:{names[2]}@4h")
    _plant(conn, f"strategy:{names[0]},rsi_ind@1h@USDJPY")
    _plant(conn, f"strategy:{names[1]},rsi_ind@1h@USDJPY")
    _plant(conn, f"strategy:{names[0]},{names[1]}@1h@USDJPY")
    _plant(conn, "indicator:rsi_ind@1h")
    metas = [_meta("rsi_ind", "indicator")]
    section = _section(conn, tmp_path, metas=metas)
    rows = _rows(section)
    assert rows["indicator (未配備) @1h"]["attempts"] == 2
    assert rows["indicator (未配備) @1h"]["outcomes"] == {
        "not_submitted": 1, "duplicate": 1}
    assert rows["indicator (未配備) @4h"]["attempts"] == 1
    assert rows["strategy:rsi_ind,未配備指標@1h@USDJPY"]["attempts"] == 2
    assert rows["strategy:未配備指標@1h@USDJPY"]["attempts"] == 1
    for row in section["rows"]:
        assert _is_family_label(row["family_key"], {"USDJPY"}), row
        if row["family_key"].startswith("indicator:"):
            assert row["family_key"] == "indicator:rsi_ind@1h"
    blob = (_prompt(conn, tmp_path, metas=metas)
            + json.dumps(_context(conn, tmp_path, metas=metas), default=str))
    for name in names:
        assert name not in blob


# --- AC-19: 時刻 -----------------------------------------------------------------

def test_ac19_section_has_only_year_month_dates(conn, tmp_path):
    _plant(conn, "indicator:rsi@1h", created="2026-03-17T04:05:06+00:00")
    _plant(conn, "indicator:rsi@4h", created="garbage-ts-9f3e")
    text = render_family_attempts(_section(conn, tmp_path, metas=[
        _meta("rsi", "indicator")]))
    assert re.findall(r"\d{4}-\d{2}-\d{2}", text) == []
    assert re.findall(r"\d{1,2}:\d{2}", text) == []
    assert re.findall(r"\d{4}-\d{2}", text) == ["2026-03"]


def test_ac19_time_canaries_never_reach_the_prompt(conn, tmp_path):
    canary_created = "2026-03-17T04:05:06+00:00"
    canary_start = datetime(2026, 2, 11, 7, 8, 9, tzinfo=timezone.utc)
    canary_finish = datetime(2026, 2, 12, 10, 11, 12, tzinfo=timezone.utc)
    mission_id = missions_store.start(conn, "improve", "codex", "gpt-5",
                                      now=canary_start, commit=True)
    backlog_id = backlog_store.add(conn, "idea", "user", canary_start)
    backlog_store.select_for_mission(conn, backlog_id, now=canary_start)
    run_id = improve_runs_store.start(conn, backlog_id, canary_start)
    conn.execute("UPDATE improvement_runs SET finished_at=? WHERE id=?",
                 (canary_finish.isoformat(), run_id))
    conn.commit()
    _plant(conn, "indicator:rsi@1h", created=canary_created,
           mission_id=mission_id)
    _plant(conn, "indicator:rsi@4h", created="garbage-ts-9f3e",
           mission_id=mission_id)
    text = _prompt(conn, tmp_path, metas=[_meta("rsi", "indicator")])
    assert "| indicator:rsi@1h | 1 | not_submitted=1 | 2026-03 |" in text
    for needle in ("2026-03-17", "04:05:06", "2026-02-11", "07:08:09",
                   "2026-02-12", "10:11:12", "garbage-ts-9f3e"):
        assert needle not in text, needle


# --- context の残存キー ------------------------------------------------------------

def test_deployed_families_key_stays_in_the_inventory(conn, tmp_path):
    ctx = _context(conn, tmp_path, metas=[_meta("rsi", "indicator")])
    assert [f["name"] for f in ctx["current_inventory"][
        "deployed_families"]] == ["rsi"]


def test_prompt_places_the_family_section_between_history_and_inventory(
        conn, tmp_path):
    text = _prompt(conn, tmp_path)
    assert (text.index("## 改善履歴") < text.index("## 仮説族の試行状況")
            < text.index("## 現行構成"))
    assert "## 配備済みの仮説族" not in text
    assert "{family_attempts}" not in text

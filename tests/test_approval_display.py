"""`approval <id>` の表示: 親が受理・記録した内容と agent の自己申告の分離、
保存形式が壊れていても落ちないこと、表示する全文字列の無害化。"""
from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.config import load_settings
from agentic_fx.loops import approval_facts
from agentic_fx.store import approvals, candidate_archives
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
LEGACY = json.loads(
    (Path(__file__).parent / "fixtures" / "approval_payloads_legacy.json"
     ).read_text(encoding="utf-8"))

INVALID = approval_facts.INVALID_FORMAT_TEXT
PARENT_HEADING = approval_facts.PARENT_HEADING
CLAIMS_HEADING = approval_facts.CLAIMS_HEADING


def _commands(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    (tmp_path / "logs").mkdir(exist_ok=True)
    cmds = Commands(
        conn=conn, state_store=StateStore(tmp_path / "s.json"),
        broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        trade_loop=MagicMock(),
        activity=ActivityLog(tmp_path / "logs" / "activity.log"),
        log_dir=tmp_path / "logs", clock=FixedClock(NOW),
        health_latch=HealthLatch())
    return conn, cmds


def _new_payload(**overrides):
    payload = {
        "name": "sma_cross", "kind": "strategy", "content_hash": "c" * 64,
        "eval_timeframe": "1h", "mission_id": 5,
        "in_sample": {"USDJPY": {"pf": 1.2, "trades": 100, "avg_r": 0.1,
                                 "max_drawdown": 0.05}},
        "holdout": {"pf": 0.9, "trades": 40, "avg_r": -0.1,
                    "max_drawdown": 0.06},
        "selection_rationale": "RATIONALE", "summary": "SUMMARY",
        "facts_version": 1,
        "parent_facts": {
            "backlog": {"id": 101, "attempts": 1, "status": "selected"},
            "prior_runs": [
                {"run_id": 8, "result": None, "approval_id": None},
                {"run_id": 7, "result": "report", "approval_id": None},
                {"run_id": 6, "result": "approval", "approval_id": 33}],
            "trials": [{"n": 1, "content_hash8": "1a2b3c4d",
                        "same_hash_as_submitted": True, "trades": 100,
                        "pf": 1.2, "avg_r": 0.1, "max_drawdown": 0.05}],
            "trials_omitted": 0, "analysis_call_count": 0},
        "agent_claims": {"selection_rationale": "RATIONALE",
                         "summary": "SUMMARY",
                         "selected_backlog_idea": "IDEA"},
    }
    payload.update(overrides)
    return payload


def _show(tmp_path, payload, *, raw=None, **row):
    conn, cmds = _commands(tmp_path)
    approval_id = approvals.create(conn, kind="plugin", payload={}, now=NOW)
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                 (raw if raw is not None else json.dumps(payload),
                  approval_id))
    for column, value in row.items():
        conn.execute(f"UPDATE approval_requests SET {column}=? WHERE id=?",
                     (value, approval_id))
    conn.commit()
    return cmds.dispatch(f"approval {approval_id}")


def _assert_clean(out):
    for ch in out:
        if ch == "\n":
            continue
        assert unicodedata.category(ch) not in ("Cc", "Cf"), repr(ch)


# ---- 新形式 ----

def test_new_format_shows_parent_section_before_claims_with_fixed_note(
        tmp_path):
    out = _show(tmp_path, _new_payload())
    lines = out.split("\n")
    parent = lines.index(PARENT_HEADING)
    claims = lines.index(CLAIMS_HEADING)
    assert parent < claims
    assert lines[parent + 1:claims] == [
        "backlog #101 (今回を含め attempts=1) status=selected",
        "過去の完了 run:",
        "  run #8: 結果なし (観測または失敗。区別できる記録がありません)",
        "  run #7: 報告として完了 (承認依頼なし)",
        "  run #6: 承認依頼 #33",
        "mission 内の backtest 1 件 (in_sample の受理分) / 分析 0 件:",
        "  1. hash=1a2b3c4d (提出と同一) trades=100 pf=1.2 avg_r=0.1 mdd=0.05",
        approval_facts.PARENT_NOTE]
    assert lines[claims + 1:claims + 4] == [
        "  自己申告: selection_rationale: RATIONALE",
        "  自己申告: summary: SUMMARY",
        "  引用: 選んだ課題の文面 (agent 起票): IDEA"]
    # 親欄は metrics の後、自己申告欄は reason 行の前
    assert lines.index("holdout: pf=0.9 trades=40 avg_r=-0.1 "
                       "max_drawdown=0.06") < parent
    assert claims < next(i for i, ln in enumerate(lines)
                         if ln.startswith("reason="))


def test_no_prior_runs_and_no_backlog_are_stated(tmp_path):
    payload = _new_payload()
    payload["parent_facts"]["backlog"] = None
    payload["parent_facts"]["prior_runs"] = []
    out = _show(tmp_path, payload)
    assert "backlog: なし" in out
    assert "過去の完了 run: なし" in out


def test_omitted_trials_are_counted_in_the_total(tmp_path):
    payload = _new_payload()
    payload["parent_facts"]["trials_omitted"] = 3
    out = _show(tmp_path, payload)
    assert "mission 内の backtest 4 件" in out
    assert "(ほか 3 件は省略)" in out


def test_trial_not_same_as_submitted_and_none_metrics(tmp_path):
    payload = _new_payload()
    payload["parent_facts"]["trials"] = [{
        "n": 1, "content_hash8": "aaaaaaaa", "same_hash_as_submitted": False,
        "trades": 0, "pf": None, "avg_r": None, "max_drawdown": 0.0}]
    out = _show(tmp_path, payload)
    assert "  1. hash=aaaaaaaa (提出と別) trades=0 pf=- avg_r=- mdd=0.0" in out


def test_claims_without_quote_have_no_quote_line(tmp_path):
    payload = _new_payload()
    del payload["agent_claims"]["selected_backlog_idea"]
    assert "引用:" not in _show(tmp_path, payload)


def test_claims_come_from_agent_claims_not_the_top_level_keys(tmp_path):
    payload = _new_payload(selection_rationale="TOP")
    out = _show(tmp_path, payload)
    assert "自己申告: selection_rationale: RATIONALE" in out
    assert "TOP" not in out


# ---- 旧形式 (実 DB の現物の形) ----

@pytest.mark.parametrize("key", sorted(LEGACY))
def test_legacy_payloads_from_the_real_db_show_legacy_line(tmp_path, key):
    payload = LEGACY[key]
    assert "facts_version" not in payload
    out = _show(tmp_path, payload)
    assert approval_facts.LEGACY_LINE in out
    assert PARENT_HEADING + " " + INVALID not in out
    assert not any(ln.startswith("backlog #") for ln in out.split("\n"))
    assert f"name={payload['name']} " in out


def test_legacy_agent_payload_keeps_old_key_claims_under_unverified_heading(
        tmp_path):
    payload = LEGACY["agent_strategy_approved_21"]
    out = _show(tmp_path, payload)
    lines = out.split("\n")
    heading = lines.index(CLAIMS_HEADING)
    assert lines[heading + 1] == (
        "  自己申告: selection_rationale: " + payload["selection_rationale"])
    assert lines[heading + 2] == "  自己申告: summary: " + payload["summary"]
    assert "in_sample USDJPY: pf=1.4440572139303938 trades=201" in out
    assert "holdout: pf=0.7624153836938286 trades=48" in out


def test_legacy_human_payload_has_no_claims_section(tmp_path):
    out = _show(tmp_path, LEGACY["human_indicator_approved_20"])
    assert approval_facts.LEGACY_LINE in out
    assert CLAIMS_HEADING not in out


@pytest.mark.parametrize("version", [None, "1", 1.0, True, [1]])
def test_non_integer_facts_versions_are_legacy_and_facts_are_not_backfilled(
        tmp_path, version):
    payload = _new_payload()
    if version is None:
        del payload["facts_version"]
    else:
        payload["facts_version"] = version
    out = _show(tmp_path, payload)
    assert approval_facts.LEGACY_LINE in out
    assert approval_facts.UNKNOWN_VERSION_LINE not in out
    assert "backlog #" not in out and "run #8" not in out
    assert "自己申告: selection_rationale: RATIONALE" in out


@pytest.mark.parametrize("version", [0, 2, 99])
def test_other_integer_facts_versions_are_shown_as_unknown_not_legacy(
        tmp_path, version):
    out = _show(tmp_path, _new_payload(facts_version=version))
    assert approval_facts.UNKNOWN_VERSION_LINE in out
    assert approval_facts.LEGACY_LINE not in out
    assert "backlog #" not in out and "run #8" not in out
    assert "自己申告: selection_rationale: RATIONALE" in out


def test_a_collection_failed_approval_is_told_apart_from_legacy(tmp_path):
    payload = _new_payload(parent_facts=None,
                           facts_error="collection_failed")
    del payload["agent_claims"]
    out = _show(tmp_path, payload)
    assert approval_facts.COLLECTION_FAILED_LINE in out
    assert approval_facts.LEGACY_LINE not in out
    assert INVALID not in out
    assert "backlog #" not in out and "run #8" not in out
    assert "自己申告: selection_rationale: RATIONALE" in out


def test_version_one_with_null_facts_and_no_error_mark_is_invalid(tmp_path):
    out = _show(tmp_path, _new_payload(parent_facts=None))
    assert f"{PARENT_HEADING} {INVALID}" in out.split("\n")
    assert approval_facts.COLLECTION_FAILED_LINE not in out


def test_prior_runs_without_a_result_are_not_called_observation_only(
        tmp_path):
    payload = _new_payload()
    payload["parent_facts"]["prior_runs"] = [
        {"run_id": 9, "result": None, "approval_id": None},
        {"run_id": 8, "result": "observation", "approval_id": None},
        {"run_id": 7, "result": "failed", "approval_id": None},
        {"run_id": 6, "result": "interrupted", "approval_id": None},
        {"run_id": 5, "result": "report_failed", "approval_id": None}]
    lines = _show(tmp_path, payload).split("\n")
    assert "  run #9: 結果なし (観測または失敗。区別できる記録がありません)" \
        in lines
    assert "  run #8: 観測のみ (承認依頼なし)" in lines
    assert "  run #7: 失敗 (mission status=failed)" in lines
    assert "  run #6: 失敗 (mission status=interrupted)" in lines
    assert "  run #5: 報告の作成に失敗 (承認依頼なし)" in lines


def test_numeric_cells_are_cut_at_the_field_limit(tmp_path):
    huge = 10 ** 400
    payload = _new_payload()
    payload["in_sample"] = {"USDJPY": {"trades": huge, "pf": 1.0}}
    payload["parent_facts"]["trials"][0]["trades"] = huge
    out = _show(tmp_path, payload)
    assert str(huge) not in out
    suffix = approval_facts.TRUNCATION_SUFFIX
    limit = approval_facts.FIELD_DISPLAY_LIMIT
    assert f"trades={str(huge)[:limit]}{suffix}" in out


# ---- 壊れた形式 ----

@pytest.mark.parametrize("raw", [
    "[]", '"text"', "null", "5", "true", "not json at all", "{",
    "[" * 100000])
def test_non_dict_payload_root_never_raises_and_shows_invalid_line(
        tmp_path, raw):
    out = _show(tmp_path, None, raw=raw, reason="human reason")
    lines = out.split("\n")
    assert lines[0].startswith("approval #")
    assert approval_facts.PAYLOAD_INVALID_LINE in lines
    assert "reason=human reason decided_by=- decided_at=-" in out


def _break(fn):
    payload = _new_payload()
    fn(payload["parent_facts"])
    return payload


PARENT_BREAKERS = {
    "root list": lambda f: None,
    "missing key": lambda f: f.pop("trials"),
    "extra key": lambda f: f.update(extra=1),
    "trials not list": lambda f: f.update(trials="x"),
    "trial extra key": lambda f: f["trials"][0].update(note="x"),
    "trial nan": lambda f: f["trials"][0].update(pf=float("nan")),
    "trial bool number": lambda f: f["trials"][0].update(trades=True),
    "run bad result": lambda f: f["prior_runs"][0].update(result="x"),
    "run approval without id": lambda f: f["prior_runs"][2].update(
        approval_id=None),
    "run report with id": lambda f: f["prior_runs"][1].update(approval_id=3),
    "backlog bad status": lambda f: f["backlog"].update(status="zzz"),
    "backlog not dict": lambda f: f.update(backlog="x"),
}


@pytest.mark.parametrize("name", sorted(PARENT_BREAKERS))
def test_broken_parent_facts_only_replace_the_parent_section(tmp_path, name):
    conn_dir = tmp_path
    payload = _break(PARENT_BREAKERS[name])
    if name == "root list":
        payload["parent_facts"] = []
    out = _show(conn_dir, payload, reason="REASON")
    lines = out.split("\n")
    assert f"{PARENT_HEADING} {INVALID}" in lines
    assert "backlog #" not in out and "run #8" not in out
    assert approval_facts.PARENT_NOTE not in out
    assert "  自己申告: selection_rationale: RATIONALE" in out
    assert "reason=REASON" in out
    assert any(ln.startswith("archive=") for ln in lines)
    assert any(ln.startswith("in_sample USDJPY:") for ln in lines)


@pytest.mark.parametrize("field,value,expected", [
    ("in_sample", [], f"in_sample: {INVALID}"),
    ("in_sample", "x", f"in_sample: {INVALID}"),
    ("in_sample", 5, f"in_sample: {INVALID}"),
    ("in_sample", {"USDJPY": []}, f"in_sample USDJPY: {INVALID}"),
    ("in_sample", {"USDJPY": {"pf": "1.2"}}, f"in_sample USDJPY: {INVALID}"),
    ("in_sample", {"USDJPY": {"trades": True}}, None),
    ("in_sample", {"USDJPY": {"pf": float("inf")}}, f"in_sample USDJPY: {INVALID}"),
    ("in_sample", {"USDJPY": {"avg_r": [1]}}, f"in_sample USDJPY: {INVALID}"),
    ("holdout", [], f"holdout: {INVALID}"),
    ("holdout", {"trades": "many"}, f"holdout: {INVALID}"),
    ("holdout", {"trades": 3, "pf": {"a": 1}}, f"holdout: {INVALID}"),
])
def test_broken_metrics_replace_only_their_own_line(
        tmp_path, field, value, expected):
    payload = _new_payload()
    payload[field] = value
    out = _show(tmp_path, payload)
    lines = out.split("\n")
    if expected is None:
        expected = f"in_sample USDJPY: {INVALID}"
    assert expected in lines
    other = "holdout" if field == "in_sample" else "in_sample"
    assert any(ln.startswith(other) and INVALID not in ln for ln in lines)
    assert PARENT_HEADING in lines and CLAIMS_HEADING in lines
    assert any(ln.startswith("archive=") for ln in lines)


def test_one_bad_pair_does_not_hide_the_other_pair(tmp_path):
    payload = _new_payload()
    payload["in_sample"] = {"USDJPY": {"pf": 1.1, "trades": 5},
                            "EURUSD": "oops"}
    lines = _show(tmp_path, payload).split("\n")
    assert "in_sample USDJPY: pf=1.1 trades=5 avg_r=- max_drawdown=-" in lines
    assert f"in_sample EURUSD: {INVALID}" in lines


# ---- 無害化 ----

HOSTILE = ("A\x1b[31m\x00\x07B‮C‪‫‬‭⁦⁧"
           "⁨⁩​‏﻿D\r\nE\tF")


def test_every_displayed_string_is_sanitized_to_one_line(tmp_path):
    forged = "x\n" + PARENT_HEADING + "\n" + CLAIMS_HEADING + "\nbacklog #9"
    payload = _new_payload(
        name=HOSTILE, content_hash=HOSTILE, eval_timeframe=HOSTILE,
        floor_warning=HOSTILE, floor_detail=HOSTILE,
        profitability_floor={HOSTILE: HOSTILE})
    payload["in_sample"] = {HOSTILE: {"pf": 1.0}}
    payload["agent_claims"] = {"selection_rationale": forged + HOSTILE,
                               "summary": forged, "selected_backlog_idea":
                               forged}
    out = _show(tmp_path, payload, reason=HOSTILE, decided_by=HOSTILE,
                decided_at=HOSTILE, status=HOSTILE, kind=HOSTILE)
    _assert_clean(out)
    lines = out.split("\n")
    assert lines.count(PARENT_HEADING) == 1
    assert lines.count(CLAIMS_HEADING) == 1
    assert not any(ln.startswith("backlog #9") for ln in lines)
    assert any(ln.startswith("  自己申告: selection_rationale: x ")
               for ln in lines)
    assert "name=A[31mBCD E F content_hash=A[31mBCD E F" in out


def test_sanitized_name_reads_as_text_without_control_characters(tmp_path):
    out = _show(tmp_path, _new_payload(name=HOSTILE))
    assert "name=A[31mBCD E F content_hash=" in out


def test_dependent_plugin_names_and_archive_path_are_sanitized(tmp_path):
    conn, cmds = _commands(tmp_path)
    payload = _new_payload(kind="indicator", name="ind", mission_id=7,
                           content_hash="h")
    approval_id = approvals.create(conn, kind="plugin", payload=payload,
                                   now=NOW)
    candidate_archives.insert(
        conn, mission_id=7, name="ind", content_hash="h",
        artifact_hash="a", archive_path="plugins/_archive/" + HOSTILE,
        pair="USDJPY", metrics={}, now=NOW)
    cmds._dependent_strategies = lambda **kw: ([HOSTILE], [HOSTILE + "2"])
    out = cmds.dispatch(f"approval {approval_id}")
    _assert_clean(out)
    assert "dependent_pinned_here=A[31mBCD E F" in out
    assert "archive=plugins/_archive/A[31mBCD E F" in out


def test_long_free_text_is_cut_with_fixed_suffix_on_one_line(tmp_path):
    payload = _new_payload()
    payload["agent_claims"]["summary"] = "あ" * 5000
    out = _show(tmp_path, payload, reason="い" * 5000)
    suffix = approval_facts.TRUNCATION_SUFFIX
    assert ("  自己申告: summary: " + "あ" * 600 + suffix) in out.split("\n")
    assert ("い" * 600 + suffix + " decided_by") in out


def test_long_single_value_fields_are_cut(tmp_path):
    out = _show(tmp_path, _new_payload(name="n" * 1000))
    first = out.split("\n")[1]
    assert first.startswith("name=" + "n" * 200 + approval_facts.TRUNCATION_SUFFIX)


def test_fullwidth_lookalikes_are_kept(tmp_path):
    payload = _new_payload()
    payload["agent_claims"]["summary"] = "＝＝＝ 親が受理・記録した内容 ＝＝＝"
    out = _show(tmp_path, payload)
    assert "  自己申告: summary: ＝＝＝ 親が受理・記録した内容 ＝＝＝" in out


def test_non_string_values_are_not_rendered_with_str(tmp_path):
    payload = _new_payload(name={"a": 1}, content_hash=["x"],
                           eval_timeframe=5)
    out = _show(tmp_path, payload)
    assert "name=- content_hash=- eval_timeframe=-" in out


def test_non_string_claim_value_shows_invalid_text(tmp_path):
    payload = _new_payload()
    payload["agent_claims"]["summary"] = {"x": 1}
    assert f"  自己申告: summary: {INVALID}" in _show(tmp_path, payload)


# ---- 決定経路の独立 ----

def _wire_plugins(tmp_path, cmds):
    plugins = tmp_path / "plugins"
    (plugins / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins
    cmds.settings = SETTINGS


@pytest.mark.parametrize("decision,expected", [
    # plugin の approve は候補の実体が要るので、候補欠損では承認されず pending のまま
    # (表示経路を通らずに backend の結果が返ることだけを見る)。
    ("approve", "pending"), ("reject because", "rejected")])
def test_decisions_do_not_use_the_display_path(
        tmp_path, monkeypatch, decision, expected):
    conn, cmds = _commands(tmp_path)
    _wire_plugins(tmp_path, cmds)
    payload = _new_payload(candidate_origin="staging",
                           candidate_path="plugins/_staging/1/sma_cross",
                           artifact_hash="a" * 64)
    approval_id = approvals.create(conn, kind="plugin", payload=payload, now=NOW)

    def boom(*a, **k):
        raise AssertionError("display path must not be called")
    monkeypatch.setattr(Commands, "_approval_detail", boom)
    monkeypatch.setattr(approval_facts, "render_facts_lines", boom)
    monkeypatch.setattr(approval_facts, "validate_parent_facts", boom)
    monkeypatch.setattr(approval_facts, "display_text", boom)

    out = cmds.dispatch(f"{decision.split()[0]} {approval_id} "
                        + " ".join(decision.split()[1:]))

    assert "エラー" not in out
    status = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                          (approval_id,)).fetchone()["status"]
    assert status == expected


def test_decision_succeeds_on_corrupt_payload(tmp_path):
    # plugin の決定は payload の name を要る。表示用の欄 (親の事実・自己申告) が壊れていても
    # 決定は通ることを見る (JSON 自体が壊れた行は決定側が fail closed で扱う)。
    conn, cmds = _commands(tmp_path)
    _wire_plugins(tmp_path, cmds)
    payload = {"name": "sma_cross", "parent_facts": "[[", "agent_claims": 5,
               "in_sample": [None], "holdout": "x"}
    approval_id = approvals.create(conn, kind="plugin", payload=payload, now=NOW)
    cmds.dispatch(f"reject {approval_id} because")
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()["status"] == "rejected"


@pytest.mark.parametrize("command", ["approve {id}", "reject {id} because"])
def test_live_trade_kind_is_refused_by_the_shell_and_stays_pending(tmp_path, command):
    conn, cmds = _commands(tmp_path)
    approval_id = approvals.create(conn, kind="live_trade", payload={}, now=NOW)
    out = cmds.dispatch(command.format(id=approval_id))
    assert "plugin 以外の kind" in out
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"


# ---- 壊れた保存形式の細部と欄ごとの上限 ----

@pytest.mark.parametrize("status", [["open"], {"a": 1}])
def test_unhashable_backlog_status_only_replaces_the_parent_section(
        tmp_path, status):
    payload = _new_payload()
    payload["parent_facts"]["backlog"]["status"] = status
    out = _show(tmp_path, payload)
    assert f"{PARENT_HEADING} {INVALID}" in out.split("\n")
    assert "  自己申告: summary: SUMMARY" in out.split("\n")


@pytest.mark.parametrize("claims", [["summary"], "summary selection_rationale",
                                    5, None])
def test_non_dict_agent_claims_show_the_claims_heading_and_an_invalid_line(
        tmp_path, claims):
    out = _show(tmp_path, _new_payload(agent_claims=claims))
    lines = out.split("\n")
    assert PARENT_HEADING in lines
    assert lines.count(CLAIMS_HEADING) == 1
    assert lines[lines.index(CLAIMS_HEADING) + 1] == (
        approval_facts.CLAIMS_INVALID_LINE)
    assert approval_facts.CLAIMS_INVALID_LINE.endswith(INVALID)
    assert "自己申告: selection_rationale" not in out


def test_missing_agent_claims_key_in_the_new_format_shows_an_invalid_line(
        tmp_path):
    payload = _new_payload()
    del payload["agent_claims"]
    lines = _show(tmp_path, payload).split("\n")
    assert lines[lines.index(CLAIMS_HEADING) + 1] == (
        approval_facts.CLAIMS_INVALID_LINE)


def test_empty_agent_claims_dict_shows_no_claims_section(tmp_path):
    out = _show(tmp_path, _new_payload(agent_claims={}))
    assert CLAIMS_HEADING not in out


def test_empty_metrics_dict_is_shown_as_a_dash_line(tmp_path):
    payload = _new_payload()
    payload["in_sample"] = {}
    payload["holdout"] = {}
    lines = _show(tmp_path, payload).split("\n")
    assert "in_sample: -" in lines
    assert "holdout: -" in lines


def test_pair_label_with_a_line_break_stays_on_one_line(tmp_path):
    payload = _new_payload()
    payload["in_sample"] = {"US\nDJPY": {"pf": 1.0, "trades": 1}}
    lines = _show(tmp_path, payload).split("\n")
    assert "in_sample US DJPY: pf=1.0 trades=1 avg_r=- max_drawdown=-" in lines


def test_floor_strings_are_cut_at_the_free_text_limit(tmp_path):
    limit = approval_facts.CLAIM_DISPLAY_LIMIT
    suffix = approval_facts.TRUNCATION_SUFFIX
    payload = _new_payload(floor_warning="w" * (limit + 100),
                           floor_detail="d" * limit,
                           profitability_floor="p" * (limit + 1))
    lines = _show(tmp_path, payload).split("\n")
    assert "floor_warning=" + "w" * limit + suffix in lines
    assert "floor_detail=" + "d" * limit in lines
    assert "profitability_floor=" + "p" * limit + suffix in lines


def test_non_string_floor_values_are_shown_as_ascii_json(tmp_path):
    payload = _new_payload(floor_detail={"理由": "日本"})
    out = _show(tmp_path, payload)
    assert 'floor_detail={"\\u7406\\u7531": "\\u65e5\\u672c"}' in out


def test_profitability_floor_text_is_sanitized(tmp_path):
    out = _show(tmp_path, _new_payload(profitability_floor="a\n" + HOSTILE))
    _assert_clean(out)
    assert "profitability_floor=a A[31mBCD E F" in out.split("\n")


@pytest.mark.parametrize("length,cut", [(300, False), (301, True)])
def test_archive_path_has_its_own_longer_limit(tmp_path, length, cut):
    conn, cmds = _commands(tmp_path)
    payload = _new_payload(kind="indicator", name="ind", mission_id=7,
                           content_hash="h")
    approval_id = approvals.create(conn, kind="plugin", payload=payload,
                                   now=NOW)
    path = "p" * length
    candidate_archives.insert(
        conn, mission_id=7, name="ind", content_hash="h",
        artifact_hash="a", archive_path=path, pair="USDJPY", metrics={},
        now=NOW)
    cmds._dependent_strategies = lambda **kw: ([], [])
    expected = ("p" * 300 + approval_facts.TRUNCATION_SUFFIX) if cut else path
    assert f"archive={expected}" in cmds.dispatch(
        f"approval {approval_id}").split("\n")


# ---- 行区切り・結合文字・サロゲート ----

NASTY = "a\u2028b\u2029c\x85d\x0be\x1cf\ud800g"
COMBINED = "e\u0301"
FORGED = "\u2028" + PARENT_HEADING + "\u2029" + CLAIMS_HEADING + "\x85"


def _assert_one_line_per_row(out):
    _assert_clean(out)
    assert out.splitlines() == out.split("\n")
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in out)
    lines = out.split("\n")
    assert sum(ln.startswith(PARENT_HEADING) for ln in lines) == 1
    assert lines.count(CLAIMS_HEADING) == 1


def _inject(key):
    value = NASTY + COMBINED + FORGED
    payload = _new_payload()
    if key == "name":
        payload["name"] = value
    elif key == "pair":
        payload["in_sample"] = {value: {"pf": 1.0}}
    else:
        payload["agent_claims"][key] = value
    return payload


@pytest.mark.parametrize("key", ["name", "pair", "selection_rationale",
                                 "summary", "selected_backlog_idea"])
def test_payload_values_with_line_separators_and_surrogates_stay_one_line(
        tmp_path, key):
    out = _show(tmp_path, _inject(key))
    _assert_one_line_per_row(out)
    assert COMBINED in out


def test_reason_with_line_separators_stays_one_line(tmp_path):
    out = _show(tmp_path, _new_payload(),
                reason=NASTY.replace("\ud800", "") + COMBINED + FORGED)
    _assert_one_line_per_row(out)
    assert COMBINED in out


def test_archive_path_with_line_separators_stays_one_line(tmp_path):
    conn, cmds = _commands(tmp_path)
    payload = _new_payload(kind="indicator", name="ind", mission_id=7,
                           content_hash="h")
    approval_id = approvals.create(conn, kind="plugin", payload=payload,
                                   now=NOW)
    candidate_archives.insert(
        conn, mission_id=7, name="ind", content_hash="h", artifact_hash="a",
        archive_path="p/" + NASTY.replace("\ud800", "") + COMBINED + FORGED,
        pair="USDJPY", metrics={}, now=NOW)
    cmds._dependent_strategies = lambda **kw: ([NASTY + FORGED], [])
    out = cmds.dispatch(f"approval {approval_id}")
    _assert_one_line_per_row(out)
    assert COMBINED in out


# ---- 省略の接尾辞 ----

def test_a_claim_cut_at_save_time_shows_the_suffix_once(tmp_path):
    suffix = approval_facts.TRUNCATION_SUFFIX
    stored = "あ" * approval_facts.CLAIM_STORE_LIMIT + suffix
    payload = _new_payload()
    payload["agent_claims"]["summary"] = stored
    out = _show(tmp_path, payload)
    line = next(ln for ln in out.split("\n")
                if ln.startswith("  自己申告: summary: "))
    assert line.count(suffix) == 1 and line.endswith(suffix)
    assert line == ("  自己申告: summary: "
                    + "あ" * approval_facts.CLAIM_DISPLAY_LIMIT + suffix)


def test_a_short_value_that_already_ends_with_the_suffix_is_not_doubled():
    suffix = approval_facts.TRUNCATION_SUFFIX
    value = "x" * (approval_facts.CLAIM_DISPLAY_LIMIT - 3) + suffix
    assert approval_facts.display_text(
        value, approval_facts.CLAIM_DISPLAY_LIMIT) == value

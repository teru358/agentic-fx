"""承認詳細に出す indicator gate の警告 (`gate_warnings` / `similar_to` /
`scale_columns`): 語 -> 文の表、共有の行生成、CLI / ops DTO / ops client の 3 面が
同じ文を出すこと、壊れた行だけ落として件数を 1 行で示し例外にしないこと、数値を出さないこと。
web 面は daemon が描画した行 (DTO の `gate_lines`) を表示するだけで、表を持たない。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops import approval_facts
from agentic_fx.store import approvals
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")

SCALE_LINE = ("警告: 価格または出来高のスケールに依存する出力 "
              "(別銘柄で挙動が変わる可能性)")


def _row(name="ema", column="value", kind="similar"):
    return {"name": name, "column": column, "kind": kind}


def _payload(**fields):
    base = {"name": "cand", "kind": "indicator", "content_hash": "c" * 64,
            "eval_timeframe": "1h"}
    base.update(fields)
    return base


# --- 語 -> 文の表 -------------------------------------------------------------

def test_every_payload_warning_word_has_exactly_one_fixed_sentence():
    assert set(codes.INDICATOR_WARNING_TEXT) == set(codes.INDICATOR_PAYLOAD_WARNING_CODES)
    for sentence in codes.INDICATOR_WARNING_TEXT.values():
        assert sentence.startswith("警告: ") and "\n" not in sentence
        assert not re.search(r"[0-9]", sentence.replace("30 本", ""))


def test_every_relation_kind_has_a_phrase_and_nothing_else_does():
    assert set(codes.INDICATOR_RELATION_TEXT) == {
        "duplicate", "monotone", "overlap", "similar"}


def test_the_scale_sentence_is_the_documented_wording():
    assert codes.INDICATOR_WARNING_TEXT["scale_dependent"] == SCALE_LINE


# --- 行生成 -------------------------------------------------------------------

def test_scale_dependent_line_carries_the_column_names():
    lines = approval_facts.gate_lines(_payload(
        gate_warnings=["scale_dependent"], scale_columns=["gap", "vol_move"]))
    assert lines == [SCALE_LINE + " (列: gap, vol_move)"]


def test_scale_dependent_without_columns_is_the_plain_sentence():
    assert approval_facts.gate_lines(_payload(gate_warnings=["scale_dependent"])) == [
        SCALE_LINE]


def test_every_word_prints_its_own_sentence_in_payload_order():
    words = ["sparse_output", "scale_dependent", "duplicate_unavailable"]
    lines = approval_facts.gate_lines(_payload(gate_warnings=words))
    assert lines == [codes.INDICATOR_WARNING_TEXT[w] for w in words]


def test_each_similar_row_prints_name_kind_and_column():
    rows = [_row("ema", "value", "similar"), _row("rsi", "rsi", "duplicate"),
            _row("atr", "a", "monotone")]
    lines = approval_facts.gate_lines(_payload(similar_to=rows))
    assert lines == ["警告: 配備済み `ema` と類似 (列: value)",
                     "警告: 配備済み `rsi` と同等 (列: rsi)",
                     "警告: 配備済み `atr` の単調変換 (列: a)"]


def test_an_overlap_row_says_partial_overlap():
    assert approval_facts.gate_lines(_payload(similar_to=[_row(kind="overlap")])) == [
        "警告: 配備済み `ema` と一部重複 (列: value)"]


def test_a_pending_partner_is_named_by_approval_id_not_as_deployed():
    lines = approval_facts.gate_lines(_payload(
        similar_to=[_row("pending-12", "value", "monotone")]))
    assert lines == ["警告: 承認待ち #12 の単調変換 (列: value)"]


def test_warnings_come_before_similar_rows():
    lines = approval_facts.gate_lines(_payload(
        gate_warnings=["indicator_similar"], similar_to=[_row()]))
    assert lines == [codes.INDICATOR_WARNING_TEXT["indicator_similar"],
                     "警告: 配備済み `ema` と類似 (列: value)"]


@pytest.mark.parametrize("payload", [
    {}, {"gate_warnings": [], "similar_to": []}, {"gate_warnings": None},
    {"unrelated": 1}])
def test_a_payload_without_findings_prints_nothing(payload):
    assert approval_facts.gate_lines(_payload(**payload)) == []


# --- 壊れた行だけ落とし、件数を 1 行で示す。例外にしない -----------------------

UNREADABLE_WARNINGS = "判読できない警告 {} 件"
UNREADABLE_SIMILAR = "判読できない関連 {} 件"

BAD_WARNINGS = [
    ("scale_dependent", 1), ({"scale_dependent": 1}, 1), (7, 1), ([1], 1), ([None], 1),
    ([["scale_dependent"]], 1), (["unknown_word"], 1),
]


@pytest.mark.parametrize("bad,count", BAD_WARNINGS)
def test_a_broken_gate_warnings_value_is_one_unreadable_line_and_keeps_the_other(bad, count):
    payload = _payload(gate_warnings=bad, similar_to=[_row()])
    assert approval_facts.gate_lines(payload) == [
        UNREADABLE_WARNINGS.format(count), "警告: 配備済み `ema` と類似 (列: value)"]
    assert approval_facts.gate_fields(payload)["gate_warnings"] == []
    assert approval_facts.gate_fields(payload)["gate_unreadable"] is True


def test_known_words_are_shown_and_only_the_unknown_ones_are_counted():
    payload = _payload(gate_warnings=["scale_dependent", "future_code", 5,
                                      "scale_dependent", "sparse_output"])
    assert approval_facts.gate_lines(payload) == [
        SCALE_LINE, codes.INDICATOR_WARNING_TEXT["sparse_output"],
        UNREADABLE_WARNINGS.format(3)]


def test_words_over_the_cap_are_counted_not_dropped_silently():
    words = list(codes.INDICATOR_WARNING_TEXT)[:9]
    lines = approval_facts.gate_lines(_payload(gate_warnings=words))
    assert lines == [codes.INDICATOR_WARNING_TEXT[w] for w in words[:8]] + [
        UNREADABLE_WARNINGS.format(1)]


BAD_SIMILAR_VALUES = ["ema", {"name": "ema"}, 3]
BAD_SIMILAR_ROWS = [
    1, "ema", None,
    {"name": "ema", "column": "value"},
    {"name": "ema", "column": "value", "kind": "similar", "corr": 0.99},
    {"name": "ema", "column": "value", "kind": "unknown"},
    {"name": "ema", "column": "value", "kind": 1},
    {"name": 1, "column": "value", "kind": "similar"},
    {"name": "EMA bad", "column": "value", "kind": "similar"},
    {"name": "ema", "column": "va lue", "kind": "similar"},
    {"name": "pending-x", "column": "value", "kind": "similar"},
]


@pytest.mark.parametrize("bad", BAD_SIMILAR_VALUES)
def test_a_similar_to_value_that_is_not_a_list_is_one_unreadable_line(bad):
    payload = _payload(gate_warnings=["sparse_output"], similar_to=bad)
    assert approval_facts.gate_lines(payload) == [
        codes.INDICATOR_WARNING_TEXT["sparse_output"], UNREADABLE_SIMILAR.format(1)]
    assert approval_facts.gate_fields(payload)["similar_to"] == []


@pytest.mark.parametrize("bad", BAD_SIMILAR_ROWS)
def test_one_broken_similar_row_does_not_hide_the_good_ones(bad):
    payload = _payload(gate_warnings=["sparse_output"],
                       similar_to=[_row("a", "x", "duplicate"), bad,
                                   _row("b", "y", "monotone")])
    assert approval_facts.gate_lines(payload) == [
        codes.INDICATOR_WARNING_TEXT["sparse_output"],
        "警告: 配備済み `a` と同等 (列: x)", "警告: 配備済み `b` の単調変換 (列: y)",
        UNREADABLE_SIMILAR.format(1)]
    fields = approval_facts.gate_fields(payload)
    assert [r["name"] for r in fields["similar_to"]] == ["a", "b"]
    assert fields["gate_unreadable"] is True


def test_similar_rows_over_the_cap_are_counted():
    lines = approval_facts.gate_lines(_payload(similar_to=[_row(f"n{i}") for i in range(5)]))
    assert lines[-1] == UNREADABLE_SIMILAR.format(2) and len(lines) == 4


@pytest.mark.parametrize("bad", [
    "gap", {"gap": 1}, [1], [None], ["Bad Name"], ["gap"] * 2, [0.99],
    ["a" * 40], [f"c{i}" for i in range(9)]])
def test_broken_scale_columns_are_left_off_the_sentence(bad):
    payload = _payload(gate_warnings=["scale_dependent"], scale_columns=bad)
    assert approval_facts.gate_lines(payload) == [SCALE_LINE]


@pytest.mark.parametrize("payload", [None, 3, "x", [], [1]])
def test_a_value_that_is_not_a_mapping_prints_nothing(payload):
    assert approval_facts.gate_lines(payload) == []
    assert approval_facts.gate_fields(payload) == {}


def test_gate_fields_carry_only_the_keys_the_payload_has():
    assert approval_facts.gate_fields(_payload()) == {}
    assert approval_facts.gate_fields(_payload(similar_to=[])) == {"similar_to": []}
    assert approval_facts.gate_fields(_payload(**FULL)) == FULL


def test_a_broken_scale_columns_value_is_marked_unreadable():
    fields = approval_facts.gate_fields(_payload(scale_columns="gap"))
    assert fields == {"scale_columns": [], "gate_unreadable": True}


def test_numbers_never_reach_the_lines():
    row = {"name": "ema", "column": "value", "kind": "similar", "corr": 0.987654}
    payload = _payload(gate_warnings=["indicator_similar"], similar_to=[row],
                       scale_columns=[0.123456], corr=0.987654)
    text = "\n".join(approval_facts.gate_lines(payload))
    assert "0.98" not in text and "0.12" not in text and "corr" not in text


# --- 3 面 (CLI / ops DTO / ops client) が同じ文を出す --------------------------

FULL = dict(gate_warnings=["scale_dependent", "indicator_similar"],
            similar_to=[_row("ema", "value", "similar"),
                        _row("pending-9", "v", "overlap")],
            scale_columns=["gap"])
EXPECTED = [SCALE_LINE + " (列: gap)",
            codes.INDICATOR_WARNING_TEXT["indicator_similar"],
            "警告: 配備済み `ema` と類似 (列: value)",
            "警告: 承認待ち #9 と一部重複 (列: v)"]


def _show(tmp_path, payload=None, *, raw=None):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    (tmp_path / "logs").mkdir(exist_ok=True)
    cmds = Commands(
        conn=conn, state_store=StateStore(tmp_path / "s.json"),
        broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)), trade_loop=MagicMock(),
        activity=ActivityLog(tmp_path / "logs" / "activity.log"),
        log_dir=tmp_path / "logs", clock=FixedClock(NOW), health_latch=HealthLatch())
    approval_id = approvals.create(conn, kind="plugin", payload={}, now=NOW)
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                 (raw if raw is not None else json.dumps(payload), approval_id))
    conn.commit()
    return cmds.dispatch(f"approval {approval_id}")


def test_cli_approval_detail_prints_the_warning_lines(tmp_path):
    out = _show(tmp_path, _payload(**FULL))
    lines = out.split("\n")
    first = lines.index(EXPECTED[0])
    assert lines[first:first + 4] == EXPECTED


def test_cli_prints_the_lines_before_the_decision_line(tmp_path):
    lines = _show(tmp_path, _payload(**FULL)).split("\n")
    assert lines.index(EXPECTED[-1]) < next(
        i for i, line in enumerate(lines) if line.startswith("reason="))


def test_cli_legacy_payload_without_the_keys_prints_no_warning_line(tmp_path):
    out = _show(tmp_path, _payload())
    assert "警告" not in out


@pytest.mark.parametrize("bad", ["x", 3, [1], {"a": 1}, [{"a": 1}]])
def test_cli_survives_broken_values_in_every_key(tmp_path, bad):
    out = _show(tmp_path, _payload(gate_warnings=bad, similar_to=bad,
                                   scale_columns=bad))
    assert out.startswith("approval #")
    assert "判読できない警告 1 件" in out and "判読できない関連 1 件" in out
    assert "警告: 配備済み" not in out


# --- web は daemon の行を表示するだけ (表の複製を持たない) ----------------------

WEB_RENDER = (Path(__file__).resolve().parents[1] / "afx-ops" / "afx_ops" / "web"
              / "render.py")


def test_the_web_package_holds_no_copy_of_the_warning_tables():
    source = WEB_RENDER.read_text(encoding="utf-8")
    assert "GATE_WARNING_TEXT" not in source and "GATE_RELATION_TEXT" not in source
    for sentence in codes.INDICATOR_WARNING_TEXT.values():
        assert sentence not in source


def test_the_shell_has_no_approval_rendering_of_its_own():
    source = (Path(__file__).resolve().parents[1] / "src" / "agentic_fx"
              / "shell.py").read_text(encoding="utf-8")
    assert "approval" not in source and "警告" not in source

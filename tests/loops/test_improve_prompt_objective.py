"""改善 mission の prompt の目的節と禁止・必須の固定文字列 (AC-16)、床の
snapshot (AC-17)、配備済みの仮説族の一覧。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.loops.improve_context import (
    build_improve_context, deployed_families, deployed_families_text)
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.plugin.loader import IndicatorRef, PluginMeta
from agentic_fx.store.db import connect, init_db

from tests.loops.test_improve_candidate_ledger import NOW

_EXAMPLE = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

OBJECTIVE_SENTENCES = (
    "配備済みと異なる市場仮説を、根拠と再現手順が明確な候補として 1 本ずつ増やす。",
    "候補の優先順位は仮説の違いと検証可能性で決め、"
    "収益指標は提出可否の床としてのみ用いる。",
    "既存仮説の調整は新規名を作らず、同名の更新として扱う。",
)

FORBIDDEN = (
    "全体収益", "PF 最大化", "標本不足", "標本 (取引数)", "評価可能",
    "30 trades", "holdout", "holdout_not_evaluable",
    "必ず実質的な変更を含めること", "試したパラメータと得られた pf",
    "avg_r を具体的に", "明確にパラメータを変え",
    "パラメータ・フィルタ・エントリ条件を",
    "変えて `run_backtest` をやり直し", "試したパラメータ群",
    "`unprofitable`", "unprofitable", "insufficient_trades",
)

REQUIRED = (
    "理由非公開。同一候補を再提出しない",
    "not_submitted",
    "`observation:family_redirect:<name>` は、配備済みの `<name>` と同じ仮説族です。新しい名前ではなく `<name>` の更新として出し直してください。",
)


def _settings(*, min_pf=1.0, positive_avg_r=True, holdout_evaluable=False):
    gate = _EXAMPLE.improve.gate.model_copy(update={
        "min_pf": min_pf, "require_positive_avg_r": positive_avg_r,
        "require_holdout_evaluable": holdout_evaluable})
    return _EXAMPLE.model_copy(update={
        "improve": _EXAMPLE.improve.model_copy(update={"gate": gate})})


class _Ctx:
    def __init__(self, tmp_path):
        self.staging_dir = tmp_path / "staging"
        self.source_snapshot_dir = tmp_path / "source"
        self.inventory_view = {"plugins": [], "pin_broken_strategies": []}


def _render(tmp_path, settings, *, inventory_metas=None):
    conn = connect(tmp_path / "p.db")
    init_db(conn)
    ctx_data = build_improve_context(
        conn, settings=settings, now=NOW, root=tmp_path,
        allowed_backlog_ids=None,
        inventory_view={"plugins": [], "pin_broken_strategies": []},
        inventory_metas=inventory_metas)
    conn.close()
    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = settings
    return loop._render_improve_mission_prompt(ctx_data, ctx=_Ctx(tmp_path))


_SETTINGS_CASES = {
    "example": _settings(),
    "strict": _settings(holdout_evaluable=True),
    "no_avg_r": _settings(positive_avg_r=False),
    "relaxed": _settings(min_pf=0.0, positive_avg_r=False),
}


@pytest.mark.parametrize("case", sorted(_SETTINGS_CASES))
def test_ac16_prompt_contains_none_of_the_forbidden_strings(tmp_path, case):
    rendered = _render(tmp_path, _SETTINGS_CASES[case])
    for text in FORBIDDEN:
        assert text not in rendered, text


@pytest.mark.parametrize("case", sorted(_SETTINGS_CASES))
def test_ac16_prompt_contains_every_required_string(tmp_path, case):
    rendered = _render(tmp_path, _SETTINGS_CASES[case])
    for sentence in OBJECTIVE_SENTENCES:
        assert sentence in rendered, sentence
    for text in REQUIRED:
        assert text in rendered, text


def test_ac16_objective_section_comes_before_the_report_sections(tmp_path):
    rendered = _render(tmp_path, _settings())
    objective = rendered.index("## 目的")
    assert objective < rendered.index("## 直近の成績レポート")
    for sentence in OBJECTIVE_SENTENCES:
        assert rendered.index(sentence) > objective


def test_ac16_floor_sentence_has_the_value_and_no_holdout_condition(tmp_path):
    rendered = _render(tmp_path, _settings(holdout_evaluable=True))
    assert "`pf >= 1.0`" in rendered
    assert "`avg_r > 0`" in rendered
    assert "提出しないでください" in rendered
    relaxed = _render(tmp_path, _settings(min_pf=0.0, positive_avg_r=False))
    assert "`pf >= 0.0`" in relaxed and "`avg_r > 0`" not in relaxed


def test_ac16_discoveries_description_names_idea_and_fact_only(tmp_path):
    rendered = _render(tmp_path, _settings())
    assert '"kind": "idea"' in rendered
    assert '"kind": "task"' not in rendered
    assert "仮説の記述として書き" in rendered


def test_ac16_no_unexpanded_placeholder_remains(tmp_path):
    rendered = _render(tmp_path, _settings())
    for token in ("{deployed_families}", "{profitability_floor_rule}",
                  "{improvement_history_table}"):
        assert token not in rendered


# --- 配備済みの仮説族の一覧 ----------------------------------------------------

def _meta(name, kind, *, timeframe="1h", pairs=("USDJPY",), indicators=()):
    return PluginMeta(
        name=name, kind=kind, path=Path("."), params={"secret_param": 7},
        timeframe=timeframe, pairs=tuple(pairs), max_bars=100,
        content_hash="c" * 64, artifact_hash="a" * 64,
        indicators=tuple(IndicatorRef(alias=p, plugin=p, params={}, pin=None)
                         for p in indicators),
        outputs=("v",) if kind == "indicator" else None)


def test_deployed_families_list_names_and_composition_only(tmp_path):
    metas = [
        _meta("rsi_boll", "strategy", indicators=("rsi", "bollinger")),
        _meta("rsi", "indicator", pairs=()),
        _meta("bollinger", "indicator", pairs=()),
        _meta("bare", "strategy"),
    ]
    rendered = _render(tmp_path, _settings(), inventory_metas=metas)
    assert "## 仮説族の試行状況" in rendered
    assert "## 配備済みの仮説族" not in rendered
    section = rendered.split("## 仮説族の試行状況")[1].split("## 現行構成")[0] \
        if rendered.index("## 仮説族の試行状況") < rendered.index("## 現行構成") \
        else rendered.split("## 仮説族の試行状況")[1]
    assert ("| strategy:bollinger,rsi@1h@USDJPY | 0 | - | - | "
            "yes (rsi_boll) |") in section
    assert "| indicator:rsi@1h | 0 | - | - | yes (rsi) |" in section
    assert "- bare (strategy): timeframe=1h, pairs=USDJPY, " \
        "indicators=[(なし)]" in section
    assert "同名の更新" in rendered
    # 成績・パラメータ値・hash は載らない
    assert "secret_param" not in section


def test_deployed_families_empty_is_explicit():
    assert deployed_families_text(deployed_families([])) == "(なし)"


# --- AC-17 -----------------------------------------------------------------

def test_ac17_approval_payload_floor_snapshot_carries_the_new_values(
        loop_min, conn, monkeypatch):
    gate = loop_min._settings.improve.gate
    monkeypatch.setattr(gate, "min_pf", 1.0)
    monkeypatch.setattr(gate, "require_positive_avg_r", True)
    monkeypatch.setattr(gate, "require_holdout_evaluable", True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()

    payload = loop_min._build_approval_payload(
        conn, name="x", kind="indicator", content_hash="c",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1,
        backlog_id=1, candidate_origin="staging", candidate_path="p",
        gate_metrics={}, output={}, now=loop_min._clock.now())

    assert payload["profitability_floor"] == {
        "min_pf": 1.0, "require_positive_avg_r": True,
        "require_holdout_evaluable": True}

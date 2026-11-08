"""afx-ops の Discord 通知の test が使う `GET /v1/improve/runs/{id}` の応答 (実経路の写し)。

終端の各結末を実際の prepare / commit で作り、`improve_run` の応答をそのまま
`afx-ops/tests/fixtures/improve_run_summary_dtos.json` と突き合わせる。afx-ops は
daemon を import できないので、その test はこの写しを読む。形が変わってこの test が
赤になったら、`AFX_REGEN_IMPROVE_DTOS=1` で写しを作り直す。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
from unittest.mock import patch

from agentic_fx.loops import improve_run_summary as run_summary
from agentic_fx.ops.contracts import Principal

from tests.loops.test_improve_baseline_commit import CANARY_METRICS, _drive
from tests.loops.test_improve_candidate_ledger import _run, _strategy
from tests.loops.test_improve_e2e import _plugin_artifact, improve_env  # noqa: F401
from tests.loops.test_improve_loop_duplicate_metrics import (
    _fake_holdout_full_metrics, _fake_in_sample_full_metrics)
from tests.loops.test_improve_notice_paths import _observation_output, _report_symlink
from tests.loops.test_improve_quality_gate import (
    FAMILY, METRICS_1, METRICS_2, PY_X, _artifact, _decided_row, _family_patch,
    _files, _hash_of, _run_strategy)

FIXTURE = (Path(__file__).resolve().parents[2] / "afx-ops" / "tests" / "fixtures"
           / "improve_run_summary_dtos.json")
WIN = {"trades": 194, "pf": 1.4981, "win_rate": 0.487, "avg_r": 0.188,
       "max_drawdown": 0.0373, "total_pnl": 100.0, "evaluable": True}
LOSS = {"trades": 60, "pf": 0.3, "win_rate": 0.2, "avg_r": -0.1,
        "max_drawdown": 0.2, "total_pnl": -50.0, "evaluable": True}
HOLDOUT_LOSS = {"trades": 53, "pf": 0.62, "win_rate": 0.3, "avg_r": -0.04,
                "max_drawdown": 0.0392, "total_pnl": -10.0, "evaluable": True}
HOLDOUT_THIN = {"trades": 13, "pf": 1.8458377992140615, "win_rate": 0.6,
                "avg_r": 0.3029234868346611, "max_drawdown": 0.02,
                "total_pnl": 5.0, "evaluable": False}
HOLDOUT_OK = {"trades": 53, "pf": 1.1, "win_rate": 0.3, "avg_r": 0.037,
              "max_drawdown": 0.0392, "total_pnl": 10.0, "evaluable": True}
OBSERVATION = ("session_ranges で *ロンドン* 開始足の逆行を検証した。"
               "[確認](https://example.com/x) @everyone <@123456> `code` "
               "を含む申告。次回は別の仮説を優先する。")


LONG_INDICATOR = "x" * 60
LONG_FAMILY = f"strategy:{LONG_INDICATOR}@1h@USDJPY"


def _strategy_run(app, root, in_sample, holdout, **kwargs):
    kwargs.setdefault("extra", [_family_patch(FAMILY)])
    return _run(app, root, _plugin_artifact("cand", kind="strategy"),
                files=_strategy(),
                in_sample=_fake_in_sample_full_metrics(in_sample),
                holdout_fn=_fake_holdout_full_metrics(holdout), **kwargs)


def _observation(app, root):
    return _run(app, root, _observation_output(OBSERVATION))


def _not_submitted(app, root):
    return _run(app, root, _observation_output("duplicate of x"))


def _submitted(app, root):
    return _strategy_run(app, root, WIN, HOLDOUT_OK)


def _submitted_improved(app, root):
    _gate, loop, ctx = _drive(app, root, "improved", "enforce")
    return loop, ctx, None


def _not_improved(app, root):
    _gate, loop, ctx = _drive(app, root, "not_improved", "enforce")
    return loop, ctx, None


def _canary_improved(app, root):
    _gate, loop, ctx = _drive(app, root, "improved", "enforce", metrics=CANARY_METRICS)
    return loop, ctx, None


def _canary_not_improved(app, root):
    _gate, loop, ctx = _drive(app, root, "not_improved", "enforce", metrics=CANARY_METRICS)
    return loop, ctx, None


def _unprofitable_in_sample(app, root):
    return _strategy_run(app, root, LOSS, HOLDOUT_OK)


def _unprofitable_holdout(app, root):
    return _strategy_run(app, root, WIN, HOLDOUT_LOSS)


def _unprofitable_holdout_report_failed(app, root):
    return _strategy_run(app, root, WIN, HOLDOUT_LOSS,
                         before_commit=_report_symlink(root))


def _long_family_submitted(app, root):
    deployed = [{"name": LONG_INDICATOR, "kind": "indicator", "timeframe": "1h",
                 "pairs": [], "indicators": []}]
    return _strategy_run(app, root, WIN, HOLDOUT_OK,
                         extra=[_family_patch(LONG_FAMILY),
                                patch("agentic_fx.loops.improve_loop.deployed_families",
                                      lambda metas: deployed)])


def _unprofitable_holdout_thin(app, root):
    app.settings.improve.gate.require_holdout_evaluable = True
    return _strategy_run(app, root, WIN, HOLDOUT_THIN)


def _unprofitable_drawdown(app, root):
    return _strategy_run(app, root, {**WIN, "max_drawdown": 0.41}, HOLDOUT_OK)


def _insufficient_trades(app, root):
    return _strategy_run(app, root, {**WIN, "trades": 16, "evaluable": False},
                         HOLDOUT_OK)


def _quality_demotion(app, root):
    _decided_row(app.conn_core, name="past", logic_hash=_hash_of(PY_X),
                 outcome="rejected_by_human")
    return _run_strategy(app, root, "cand", metrics=METRICS_2)


SCENARIOS = {
    "observation": _observation,
    "not_submitted": _not_submitted,
    "submitted": _submitted,
    "submitted_improved": _submitted_improved,
    "not_improved": _not_improved,
    "canary_improved": _canary_improved,
    "canary_not_improved": _canary_not_improved,
    "unprofitable_in_sample": _unprofitable_in_sample,
    "unprofitable_holdout": _unprofitable_holdout,
    "unprofitable_holdout_report_failed": _unprofitable_holdout_report_failed,
    "long_family_submitted": _long_family_submitted,
    "unprofitable_holdout_thin": _unprofitable_holdout_thin,
    "unprofitable_drawdown": _unprofitable_drawdown,
    "insufficient_trades": _insufficient_trades,
    "quality_demotion": _quality_demotion,
}


def _dto(tmp_path_factory_env, name):
    app, root = tmp_path_factory_env
    _loop, ctx, _seeded = SCENARIOS[name](app, root)
    data = app.ops.improve_run(Principal.OPERATOR, ctx.mission_id)
    return json.loads(json.dumps(data, ensure_ascii=False))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_ending_matches_the_dto_afx_ops_is_tested_with(improve_env, name):
    dto = _dto(improve_env, name)
    summary = dto["summary"]
    assert summary["state"] == "ok"
    jsonschema.validate(summary["items"], run_summary.ITEMS_SCHEMA)
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
    if os.environ.get("AFX_REGEN_IMPROVE_DTOS") == "1":
        recorded[name] = dto
        recorded["_canary_metrics"] = CANARY_METRICS
        FIXTURE.write_text(json.dumps(dict(sorted(recorded.items())), ensure_ascii=False,
                                      indent=1) + "\n", encoding="utf-8")
        return
    assert recorded[name] == dto
    assert recorded["_canary_metrics"] == CANARY_METRICS


def test_a_run_whose_report_failed_still_keeps_the_floor_but_says_the_report_failed(improve_env):
    dto = _dto(improve_env, "unprofitable_holdout_report_failed")
    assert (dto["snapshot"]["result_code"], dto["snapshot"]["reason_code"]) \
        == ("gate_failed", "unprofitable")
    assert dto["report_state"] == "failed"
    assert "floor" in dto["summary"]["items"]


def test_a_family_key_longer_than_a_plugin_name_is_kept_in_the_handoff(improve_env):
    dto = _dto(improve_env, "long_family_submitted")
    handoff = dto["summary"]["items"]["handoff"]
    assert handoff["family"] == LONG_FAMILY and len(LONG_FAMILY) > 64


def test_every_comparison_pair_says_whether_each_side_was_evaluable(improve_env):
    dto = _dto(improve_env, "submitted_improved")
    pairs = dto["summary"]["items"]["comparison"]["pairs"]
    assert pairs and all(
        type(side["evaluable"]) is bool
        for pair in pairs for side in (pair["candidate"], pair["deployed"]))

"""analyze_corr の request schema は親の検証表から作られ、食い違わない。"""
from __future__ import annotations

import jsonschema
import pytest

from agentic_fx.backtest import analysis
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs


class _Ledger:
    def record(self, **_k):
        return None


def _tool():
    tools = build_improve_rpc_tooldefs(
        ledger=_Ledger(), run_backtest_handler=lambda **k: {},
        analyze_corr_handler=lambda request: {}, staging_dir=None)
    return next(t for t in tools if t.name == "analyze_corr")


def _branches():
    return _tool().parameters["properties"]["request"]["anyOf"]


def test_one_branch_per_kind_with_required_equal_to_key_set():
    branches = _branches()
    assert len(branches) == len(analysis._REQUEST_SCHEMA)
    got = {}
    for b in branches:
        (kind,) = b["properties"]["kind"]["enum"]
        got[kind] = set(b["required"])
        assert set(b["properties"]) == set(b["required"])
        assert b["additionalProperties"] is False
    assert got == {k: set(v) for k, v in analysis._REQUEST_SCHEMA.items()}


def test_enums_come_from_analysis_constants():
    for b in _branches():
        p = b["properties"]
        assert p["timeframe"]["enum"] == list(analysis.TIMEFRAMES)
        if "window" in p:
            assert p["window"]["enum"] == list(analysis.WINDOWS)


@pytest.mark.parametrize("request_", [
    {"kind": "corr_matrix", "timeframe": "1h"},
    {"kind": "rolling_corr_summary", "a": "X", "b": "Y",
     "timeframe": "4h", "window": 60},
    {"kind": "lead_lag", "a": "X", "b": "Y", "timeframe": "1d"},
])
def test_valid_forms_pass_registry_validation(request_):
    jsonschema.validate({"request": request_}, _tool().parameters)


@pytest.mark.parametrize("request_", [
    {"kind": "corr_matrix"},
    {"kind": "corr_matrix", "timeframe": "1h", "a": "X"},
    {"kind": "lead_lag", "a": "X", "b": "Y", "timeframe": "5m"},
    {"kind": "rolling_corr_summary", "a": "X", "b": "Y",
     "timeframe": "1h", "window": 30},
    {"kind": "nope", "timeframe": "1h"},
])
def test_wrong_forms_fail_validation(request_):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"request": request_}, _tool().parameters)

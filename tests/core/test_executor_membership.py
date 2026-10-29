"""発注対象の membership: `settings.pairs` に無い pair の open は executor が
quote / spec / 換算 / broker のどれにも触れる前に `not_tradable_symbol` で拒否する。

tool / schema 側の拒否 (trade-intent schema の enum) とは別の防御。"""
from __future__ import annotations

import pytest

from agentic_fx.core.executor import ExecutionSnapshot, NotTradableSymbol
from agentic_fx.core.contracts import BrokerResult, OrderStatus
from agentic_fx.loops.summary import trade_intent_schema
from agentic_fx.store import orders

from tests.core.test_executor_snapshot import (
    NOW, QUOTE, SETTINGS, SPECS, _insert_intent, _make_executor, _open_intent,
    _rate_fn, _start_trade_mission,
)

REFERENCE_PAIR = "JP225"


class _Calls:
    def __init__(self):
        self.quote = []
        self.spec = []
        self.rate = []
        self.submit = []

    def quote_fn(self, pair):
        self.quote.append(pair)
        return QUOTE

    def spec_fn(self, pair):
        self.spec.append(pair)
        return SPECS[pair]

    def rate_fn(self, ccy, account_ccy, now, **kw):
        self.rate.append(ccy)
        return _rate_fn(ccy, account_ccy, now, **kw)


class _Broker:
    def __init__(self, calls):
        self.calls = calls

    def submit(self, row, *, entry_price):
        self.calls.submit.append(row)
        return BrokerResult(status="rejected", message="x")


def _executor(tmp_path):
    calls = _Calls()
    ex = _make_executor(tmp_path, quote_fn=calls.quote_fn, spec_fn=calls.spec_fn,
                        rate_fn=calls.rate_fn, broker=_Broker(calls))
    return ex, calls


def _assert_untouched(ex, calls):
    assert calls.quote == []
    assert calls.spec == []
    assert calls.rate == []
    assert calls.submit == []
    assert orders.list_by_status(ex.conn, *list(OrderStatus)) == []


def _assert_recorded_rejection(ex, iid):
    row = ex.conn.execute("SELECT gate_result, reject_reason, reject_category "
                          "FROM trade_intents WHERE id=?", (iid,)).fetchone()
    assert dict(row) == {"gate_result": "rejected",
                         "reject_reason": "not_tradable_symbol",
                         "reject_category": "execution"}


def test_reference_symbol_is_not_a_configured_pair():
    assert REFERENCE_PAIR not in SETTINGS.pairs


def test_handle_intent_open_is_rejected_before_any_io(tmp_path):
    ex, calls = _executor(tmp_path)
    mid = _start_trade_mission(ex.conn)

    out = ex.handle_intent(_open_intent(pair=REFERENCE_PAIR), mid)

    assert out == {"result": "rejected", "order_id": None,
                   "reasons": ["not_tradable_symbol"]}
    _assert_untouched(ex, calls)
    iid = ex.conn.execute("SELECT id FROM trade_intents").fetchone()["id"]
    _assert_recorded_rejection(ex, iid)


def test_open_method_rejects_before_any_io(tmp_path):
    ex, calls = _executor(tmp_path)
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair=REFERENCE_PAIR)
    iid = _insert_intent(ex.conn, mid, intent)

    out = ex._open(intent, iid)

    assert out["reasons"] == ["not_tradable_symbol"]
    _assert_untouched(ex, calls)
    _assert_recorded_rejection(ex, iid)


def test_gather_open_snapshot_refuses_before_any_io(tmp_path):
    ex, calls = _executor(tmp_path)

    with pytest.raises(NotTradableSymbol, match="not_tradable_symbol"):
        ex.gather_open_snapshot(_open_intent(pair=REFERENCE_PAIR),
                                exposure_pairs=["USDJPY"])

    _assert_untouched(ex, calls)


def test_open_from_snapshot_rejects_before_any_io(tmp_path):
    ex, calls = _executor(tmp_path)
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair=REFERENCE_PAIR)
    iid = _insert_intent(ex.conn, mid, intent)
    forged = ExecutionSnapshot(
        quote=QUOTE, spec=SPECS["USDJPY"],
        specs_by_pair={REFERENCE_PAIR: SPECS["USDJPY"]},
        rates={"USD": _rate_fn("USD", "JPY", NOW), "JPY": _rate_fn("JPY", "JPY", NOW)},
        captured_at=NOW)

    out = ex.open_from_snapshot(intent, iid, forged, max_snapshot_age_sec=999.0)

    assert out["reasons"] == ["not_tradable_symbol"]
    _assert_untouched(ex, calls)
    _assert_recorded_rejection(ex, iid)


def test_guard_precedes_the_snapshot_barrier_checks(tmp_path):
    """gather 時点では許されていた pair が commit-core 時点で外れていても、
    鮮度や coverage の判定より前に membership で拒否される。"""
    ex, calls = _executor(tmp_path)
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair=REFERENCE_PAIR)
    iid = _insert_intent(ex.conn, mid, intent)
    wider = SETTINGS.model_copy(update={"pairs": [*SETTINGS.pairs, REFERENCE_PAIR]})
    ex.settings = wider
    ex.spec_fn = lambda p: SPECS["USDJPY"]
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    ex.settings = SETTINGS
    calls.quote.clear(); calls.spec.clear(); calls.rate.clear()

    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=-1.0)

    assert out["reasons"] == ["not_tradable_symbol"]
    assert calls.submit == []
    assert orders.list_by_status(ex.conn, *list(OrderStatus)) == []


@pytest.mark.parametrize("pair", [p for p in SETTINGS.pairs])
def test_configured_pair_path_is_unchanged(tmp_path, pair):
    ex, calls = _executor(tmp_path)
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair=pair)
    iid = _insert_intent(ex.conn, mid, intent)

    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)

    assert out["reasons"] != ["not_tradable_symbol"]
    assert calls.quote == [pair]
    assert pair in calls.spec
    assert len(calls.submit) == 1


def test_schema_enum_stays_the_configured_pairs():
    schema = trade_intent_schema(list(SETTINGS.pairs))
    assert schema["properties"]["pair"] == {"enum": list(SETTINGS.pairs)}
    assert REFERENCE_PAIR not in schema["properties"]["pair"]["enum"]


def _example_text():
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "config"
            / "settings.yaml.example").read_text(encoding="utf-8")


def test_surplus_pair_rule_key_warns_once_and_the_example_still_loads(caplog):
    import logging

    from agentic_fx.config import load_settings_text, warn_surplus_pair_rules

    with caplog.at_level(logging.WARNING, logger="agentic_fx.config"):
        settings = load_settings_text(_example_text())
        assert caplog.records == []
        surplus = warn_surplus_pair_rules(settings)

    assert surplus == sorted(set(settings.risk.pair_rules) - set(settings.pairs))
    assert surplus, "example must carry a rule for a pair outside `pairs`"
    lines = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1
    assert all(p in lines[0].getMessage() for p in surplus)
    assert "pair_rules" in lines[0].getMessage()
    assert settings.pairs == ["USDJPY"]


def test_no_warning_without_surplus_pair_rule(caplog):
    import logging

    import yaml

    from agentic_fx.config import load_settings_text, warn_surplus_pair_rules

    data = yaml.safe_load(_example_text())
    data["risk"]["pair_rules"] = {"USDJPY": data["risk"]["pair_rules"]["USDJPY"]}
    settings = load_settings_text(yaml.safe_dump(data))
    with caplog.at_level(logging.WARNING, logger="agentic_fx.config"):
        assert warn_surplus_pair_rules(settings) == []
    assert caplog.records == []


def test_daemon_startup_emits_the_surplus_pair_rule_warning():
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "agentic_fx" / "service.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "warn_surplus_pair_rules"]
    assert len(calls) == 1

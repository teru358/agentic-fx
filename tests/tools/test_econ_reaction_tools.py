"""get_econ_reactions: floor, pair scope, and the surfaces that may or may not list it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.analytics.econ_reactions import WindowValue
from agentic_fx.config import load_settings
from agentic_fx.store import econ_reaction_values as rv
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import econ_reaction_tools
from agentic_fx.tools.registry import ToolRegistry

REPO = Path(__file__).resolve().parents[2]
PAIRS = {"USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD")}
BASE = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _settings(pairs=("USDJPY", "EURUSD"), watch=("JP225",)):
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    settings.pairs = list(pairs)
    settings.datafeed.watch_symbols = list(watch)
    return settings


def _seed(conn, *, currency, pair, name, count, resolved=None):
    resolved = count if resolved is None else resolved
    for i in range(count):
        ts = (BASE + timedelta(days=i, hours=len(name))).isoformat()
        event = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
                             (ts, currency, name, 3)).lastrowid
        set_id = conn.execute(
            "INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,state,"
            "created_at,completed_at,rule_version) VALUES(?,?,?,?,?,'complete',?,?,1)",
            (event, pair, ts, (BASE + timedelta(days=i + 1)).isoformat(), 255, ts, ts)).lastrowid
        conn.commit()
        if i < resolved:
            values = tuple(WindowValue(w, "resolved", 10.0 + i, -2.0, 2.0 + i, 1.0) for w in (15, 60, 240))
        else:
            values = tuple(WindowValue(w, "unresolvable", None, None, None, None) for w in (15, 60, 240))
        assert rv.write_set_tx(conn, set_id=set_id, values=values, keys=[(event, currency)],
                               now=BASE).committed
    conn.commit()


def _tool(conn, settings=None):
    settings = settings or _settings()
    [tool] = econ_reaction_tools.build(conn, settings, PAIRS)
    return tool


def _conn(tmp_path):
    conn = connect(tmp_path / "r.db")
    init_db(conn)
    return conn


def test_a_bundle_below_the_floor_shows_identity_and_insufficient_only(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, currency="USD", pair="USDJPY", name="CPI", count=19)
    _seed(conn, currency="JPY", pair="USDJPY", name="Rate", count=20)
    rows = _tool(conn).func(pair="USDJPY")
    usd = [r for r in rows if r["currency"] == "USD"]
    jpy = [r for r in rows if r["currency"] == "JPY"]
    assert usd and all(set(r) == {"currency", "kind", "key", "window_min", "status"}
                       and r["status"] == "insufficient" for r in usd)
    assert {r["window_min"] for r in usd} == {15, 60, 240}
    assert jpy and all(r["status"] == "ok" and r["n"] == 20 for r in jpy)
    one = next(r for r in jpy if r["kind"] == "name" and r["window_min"] == 60)
    assert one["range_median"] == pytest.approx(19.5) and "range_p75" in one
    json.dumps(rows)


def test_unresolvable_samples_do_not_count_towards_the_floor(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, currency="JPY", pair="USDJPY", name="Rate", count=21, resolved=19)
    rows = _tool(conn).func(pair="USDJPY")
    assert rows and all(r["status"] == "insufficient" for r in rows)


def test_the_pair_limits_the_bundles_to_its_two_currencies(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, currency="USD", pair="USDJPY", name="CPI", count=20)
    _seed(conn, currency="JPY", pair="USDJPY", name="Rate", count=20)
    _seed(conn, currency="EUR", pair="EURUSD", name="HICP", count=20)
    tool = _tool(conn)
    assert {r["currency"] for r in tool.func(pair="USDJPY")} == {"USD", "JPY"}
    assert {r["currency"] for r in tool.func(pair="EURUSD")} == {"EUR", "USD"}


@pytest.mark.parametrize("pair", ["JP225", "GBPJPY", "", "usdjpy"])
def test_a_pair_outside_settings_pairs_including_watch_symbols_is_refused(tmp_path, pair):
    tool = _tool(_conn(tmp_path), _settings(watch=("JP225",)))
    assert tool.parameters["properties"]["pair"]["enum"] == ["USDJPY", "EURUSD"]
    with pytest.raises(ValueError):
        tool.func(pair=pair)
    registry = ToolRegistry()
    registry.register(tool)
    assert "error" in json.loads(registry.execute("get_econ_reactions", {"pair": pair},
                                                   ["get_econ_reactions"]))


def test_the_description_states_it_is_not_a_forecast_and_names_the_floor_and_unit():
    [tool] = econ_reaction_tools.build(None, _settings(), PAIRS)
    assert tool.name == "get_econ_reactions"
    for phrase in ("予測ではない", "20 件未満は数値を出さない", "pips", "15 / 60 / 240"):
        assert phrase in tool.description


def test_the_tool_reads_through_a_read_only_connection(tmp_path):
    from agentic_fx.store.db import connect_readonly
    conn = _conn(tmp_path)
    _seed(conn, currency="JPY", pair="USDJPY", name="Rate", count=20)
    conn.close()
    ro = connect_readonly(tmp_path / "r.db")
    assert any(r["status"] == "ok" for r in _tool(ro).func(pair="USDJPY"))

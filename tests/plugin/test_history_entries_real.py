"""履歴検査を差し替えず、各入口が共有 gate へ渡す引数 (dataset・pairs・now・root 基準の
無足日一覧) ごと実 sqlite の履歴で検査を通す。

入口 = 改善の提出 / 人間 submit / bless / 配備中の再評価。実 backtest だけは差し替える
(履歴検査の手前までの経路と、検査より後の証跡の運搬は本物)。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agentic_fx.backtest import history_evidence as ev
from agentic_fx.plugin import switch
from agentic_fx.store import approvals as approvals_store
from tests.history_support import fill_history, hole_day, write_no_bars
from tests.plugin.test_approval_payload_common_contract import (
    SETTINGS as BASE_SETTINGS, _conn, _fake_pytest_ok)
from tests.plugin.test_strategy_gate import _metrics

UTC = timezone.utc
SOURCE = "mt5"
TUESDAY = datetime(2026, 11, 10, 12, 0, 20, tzinfo=UTC)
WEEKEND = datetime(2026, 11, 8, 3, 17, 42, tzinfo=UTC)
NOWS = [pytest.param(TUESDAY, id="weekday"), pytest.param(WEEKEND, id="across-weekend")]
HISTORY_KINDS = {"history_stale", "history_gap"}


def _settings():
    settings = BASE_SETTINGS.model_copy(deep=True)
    settings.backtest.holdout_months = 1
    return settings


@pytest.fixture
def gate_env(monkeypatch):
    """実 backtest・pytest・intent source だけを差し替える。in-sample の pf は `pf` で変える。"""
    state = {"pf": 1.5, "holdout": 0}
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: _metrics(trades=40, pf=state["pf"]))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: state.__setitem__("holdout", state["holdout"] + 1)
        or _metrics(trades=40, pf=1.5))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: MagicMock(close=lambda: None, cpu_sec=0.0))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.require_reference_history",
        lambda *a, **kw: None)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    return state


def _write_strategy(base, name, *, pairs=("USDJPY",)):
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(
        "def evaluate(df, indicators, signals, params):\n"
        "    return {'action': 'hold', 'rationale': 'x'}\n")
    (d / "config.yaml").write_text(
        f"kind: strategy\ntimeframe: 1h\npairs: [{', '.join(pairs)}]\n"
        "exit_mode: levels\nmax_bars: 200\n")
    (d / "test_plugin.py").write_text("def test_x():\n    pass\n")
    return d


def _requests(conn):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM approval_requests").fetchall()]


# ---- 人間 submit / bless -------------------------------------------------------

def _submit_world(tmp_path, *, pairs=("USDJPY",)):
    root = tmp_path / "s"
    plugins = root / "plugins"
    (plugins / ".locks").mkdir(parents=True)
    conn = _conn(root)
    staging = plugins / "_staging" / "1"
    _write_strategy(staging, "strat_b", pairs=pairs)
    return root, conn, lambda now: switch.submit_candidate(
        conn, name="strat_b", staging_dir=staging, candidate_origin="staging", mission_id=1,
        backlog_id=None, settings=_settings(), now=now)


def _bless_world(tmp_path, *, pairs=("USDJPY",)):
    root = tmp_path / "b"
    plugins = root / "plugins"
    (plugins / ".locks").mkdir(parents=True)
    conn = _conn(root)
    _write_strategy(plugins / "_human", "strat_c", pairs=pairs)
    return root, conn, lambda now: switch.bless_candidate(
        conn, name="strat_c", human_dir=plugins / "_human" / "strat_c", settings=_settings(),
        now=now, decided_by="human_cli")


WORLDS = [pytest.param(_submit_world, id="submit"), pytest.param(_bless_world, id="bless")]


@pytest.mark.real_history_guard
@pytest.mark.parametrize("now", NOWS)
@pytest.mark.parametrize("world", WORLDS)
def test_a_healthy_history_passes_and_the_request_carries_the_evidence(
        tmp_path, gate_env, world, now):
    root, conn, call = world(tmp_path)
    fill_history(conn, source=SOURCE, pairs=["USDJPY"], now=now)
    approval_id = call(now)
    payload = json.loads(conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                                      (approval_id,)).fetchone()[0])
    evidence = payload["history_guard"]
    assert evidence["verdict"] == "ready" and evidence["pairs"] == ["USDJPY"]
    assert evidence["checked_at"] == now.isoformat()
    assert evidence["dataset"] == {"source": SOURCE, "base_interval": "5m"}
    assert ev.verify_evidence(evidence, eval_source=SOURCE, base_interval="5m",
                              pairs=["USDJPY"], no_bars_path=ev.no_bars_path(root)).ok


@pytest.mark.real_history_guard
@pytest.mark.parametrize("now", NOWS)
@pytest.mark.parametrize("kind", ["history_stale", "history_gap"])
@pytest.mark.parametrize("world", WORLDS)
def test_a_stale_or_gapped_history_stops_with_its_fixed_reason_and_files_nothing(
        tmp_path, gate_env, world, kind, now):
    root, conn, call = world(tmp_path)
    fill_history(conn, source=SOURCE, pairs=["USDJPY"], now=now,
                 scenario="stale" if kind == "history_stale" else "gap")
    with pytest.raises(ValueError) as caught:
        call(now)
    assert kind in str(caught.value)
    assert _requests(conn) == []
    assert gate_env["holdout"] == 0


@pytest.mark.real_history_guard
def test_bless_cannot_bypass_the_history_check_even_when_the_floor_only_warns(
        tmp_path, gate_env):
    gate_env["pf"] = 0.4
    root, conn, call = _bless_world(tmp_path)
    fill_history(conn, source=SOURCE, pairs=["USDJPY"], now=TUESDAY, scenario="stale")
    with pytest.raises(ValueError, match="history_stale"):
        call(TUESDAY)
    assert _requests(conn) == []


@pytest.mark.real_history_guard
@pytest.mark.parametrize("world", WORLDS)
def test_the_primary_pair_comes_from_the_candidates_pairs_not_the_settings(
        tmp_path, gate_env, world):
    """settings.pairs は USDJPY。候補の pair (EURUSD) だけが不良でも止まる。"""
    root, conn, call = world(tmp_path, pairs=("EURUSD",))
    fill_history(conn, source=SOURCE, pairs=["USDJPY"], now=TUESDAY)
    fill_history(conn, source=SOURCE, pairs=["EURUSD"], now=TUESDAY, scenario="stale")
    with pytest.raises(ValueError, match="history_stale"):
        call(TUESDAY)
    assert _requests(conn) == []


@pytest.mark.real_history_guard
@pytest.mark.parametrize("world", WORLDS)
def test_the_no_bars_list_is_read_from_the_entrys_own_root(tmp_path, gate_env, world):
    root, conn, call = world(tmp_path)
    fill_history(conn, source=SOURCE, pairs=["USDJPY"], now=TUESDAY, scenario="gap")
    day = hole_day(TUESDAY)[0].date()
    write_no_bars(root, source=SOURCE, symbol="USDJPY", day=day)
    approval_id = call(TUESDAY)
    payload = json.loads(conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                                      (approval_id,)).fetchone()[0])
    entries = payload["history_guard"]["no_bars_entries"]
    assert [(e["symbol"], e["date_utc"]) for e in entries] == [("USDJPY", day.isoformat())]

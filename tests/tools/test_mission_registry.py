"""build_mission_registry (プラン 8 worker 基盤 — 設計書 §4.2)。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.tools.mission_registry import build_mission_registry

SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


class FakeEmbedding:
    """決定論的 fake embedding (ネットワーク・モデル DL 不要)。"""

    def __call__(self, input):  # noqa: A002
        out = []
        for text in input:
            h = hashlib.sha256(text.encode()).digest()
            out.append([b / 255.0 for b in h[:16]])
        return out

    def embed_query(self, input):  # noqa: A002
        return self(input)

    def name(self):
        return "fake"


def _clock():
    return FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))


def test_build_mission_registry_registers_all_trade_tools(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=activity)

    names = set(registry.names())
    for expected in ("get_ohlcv", "get_indicators", "search_news",
                     "get_econ_calendar", "get_positions", "get_account",
                     "get_recent_reflections", "search_reflections",
                     "get_signals"):
        assert expected in names, f"{expected} missing from registry"


def test_build_mission_registry_econ_calendar_does_not_touch_activity(tmp_path):
    """econ.upcoming() (get_econ_calendar が呼ぶ) は self.activity に触れない
    — 子プロセスが activity=None 相当の最小構成で呼んでも安全なことの
    構造的な確認 (worker.py が構築するときの前提)。

    I7 対応: `ToolRegistry.execute` はツール内で送出された全例外を
    `json.dumps({"error": ...})` に変換して返す (`registry.py:63-73`)
    ため、`result is not None` は常に真になり恒真テストになっていた
    (exploding activity が実際に呼ばれて `AssertionError` が飛んでも、
    その例外は registry に握り潰されて緑のまま通ってしまう)。ここでは
    (a) `activity.write` を呼んだかどうかを例外ではなく明示カウンタで
    記録し、(b) `result` を JSON decode して `list` 型 (`get_econ_calendar`
    の正常戻り値の型) であることを確認する — decode 結果が
    `{"error": ...}` の dict なら型不一致で確実に落ちる。
    """
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())

    class CountingActivity:
        def __init__(self) -> None:
            self.write_calls = 0

        def write(self, *a, **k):
            self.write_calls += 1

    activity = CountingActivity()
    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=activity)
    result = registry.execute("get_econ_calendar", {"days": 1}, ["get_econ_calendar"])

    parsed = json.loads(result)
    assert isinstance(parsed, list), f"expected list payload, got: {result}"
    assert activity.write_calls == 0


def test_build_mission_registry_readonly_skips_bar_cache_write(tmp_path):
    """CR-4 対応: `readonly=True` で構築した registry の `get_ohlcv` は
    RO 接続 (`connect_readonly`) の下でも `ohlcv.upsert_cache_bars` の書込を
    スキップして成功する — 子プロセス (`connect_readonly` で開いた conn)
    が `get_ohlcv` を呼んでも `sqlite3.OperationalError: attempt to write
    a readonly database` にならないことの配線ピン。"""
    from unittest.mock import patch

    from agentic_fx.core.contracts import Bar
    from agentic_fx.datafeed import sources
    from agentic_fx.store import ohlcv
    from agentic_fx.store.db import connect_readonly

    db_path = tmp_path / "x.db"
    rw_conn = connect(db_path)
    init_db(rw_conn)
    rw_conn.commit()

    def _fresh_bars():
        from datetime import timedelta
        step = timedelta(minutes=1)
        # Clock is at 2026-08-04 12:00, so generate bars ending close to that time
        start = datetime(2026, 8, 4, 11, 30, tzinfo=timezone.utc)
        return [Bar("USDJPY", "1m", start + step * i,
                    148.0, 148.1, 147.9, 148.05, 10) for i in range(30)]

    ro_conn = connect_readonly(db_path)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    registry = build_mission_registry(
        "trade", ro_conn, SETTINGS, _clock(), rag,
        activity=ActivityLog(tmp_path / "logs" / "activity.log"), readonly=True)

    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        result = registry.execute(
            "get_ohlcv", {"pair": "USDJPY", "timeframe": "1m"}, ["get_ohlcv"])

    parsed = json.loads(result)
    # get_ohlcv の正常戻り値は list[dict] (market_tools.py 45-49 行)。RO
    # 接続で書込が実際に走っていれば OperationalError が
    # `{"error": ...}` の dict に化けて型不一致で検出される。
    assert isinstance(parsed, list) and len(parsed) == 30, result
    # 念のため RW 接続からも cache が空のままであることを確認する
    # (write skip の直接証跡)。
    assert ohlcv.load_cache_bars(rw_conn, "USDJPY", "1m", source="yfinance") == []


def test_build_mission_registry_provider_and_readonly_guard(tmp_path):
    """provider と readonly=True の併用は ValueError で即座に fail closed する
    (sonnet I-1)。子プロセス (mission_worker.py) が誤って provider を渡して
    も、guard で検出される。"""
    from agentic_fx.datafeed.price_provider import PriceProvider

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    # provider を注入して build_mission_registry を呼ぶ場合、readonly=True は併用禁止
    injected_provider = PriceProvider(conn, SETTINGS, _clock())

    with pytest.raises(ValueError, match="provider と readonly=True の併用は禁止"):
        build_mission_registry(
            "trade", conn, SETTINGS, _clock(), rag,
            activity=activity, provider=injected_provider, readonly=True)

    # provider なし、readonly=True は OK (子プロセスの正常系)
    registry_ro = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag,
        activity=activity, readonly=True)
    assert "get_ohlcv" in registry_ro.names()

    # provider あり、readonly=False は OK (親プロセスの正常系)
    registry_rw = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag,
        activity=activity, provider=injected_provider, readonly=False)
    assert "get_ohlcv" in registry_rw.names()


def test_build_mission_registry_improve_excludes_all_trade_tools(tmp_path):
    """improve registry には取引 registry のツールが 1 つも無い (§3.4 表「無いもの」)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir()
    source_snapshot_dir.mkdir()

    registry = build_mission_registry(
        "improve", conn, SETTINGS, _clock(), rag, activity=activity,
        staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})
    names = set(registry.names())
    forbidden = {"get_ohlcv", "get_indicators", "get_signals", "get_econ_calendar",
                 "place_intent", "bless"}
    assert names & forbidden == set()


def test_build_mission_registry_improve_has_research_and_staging_and_rpc_tools(tmp_path):
    """improve registry に research/staging/RPC tools が揃っている。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir()
    source_snapshot_dir.mkdir()

    registry = build_mission_registry(
        "improve", conn, SETTINGS, _clock(), rag, activity=activity,
        staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})
    names = set(registry.names())
    assert {"web_search", "fetch_article", "list_staging", "read_staging_file",
            "write_staging_file", "read_plugin_source", "run_plugin_tests",
            "run_backtest", "analyze_corr"} <= names


def test_build_mission_registry_improve_wires_rpc_handlers_by_tool_not_swapped(tmp_path):
    """M11 (段 0 Important): improve 分岐で `run_backtest_handler` と
    `analyze_corr_handler` を入れ替える変異が red になる pin —
    `test_..._has_research_and_staging_and_rpc_tools` はツール名の存在
    しか見ておらず配線 (どのツールがどの handler を呼ぶか) を検証して
    いなかった。区別可能な戻り値を持つ 2 handler を渡し、各ツール名から
    呼んだときに対応する handler の結果が返ることを見る。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir()
    source_snapshot_dir.mkdir()
    candidate_dir = staging_dir / "n"
    candidate_dir.mkdir()
    (candidate_dir / "config.yaml").write_text("kind: strategy\n")

    registry = build_mission_registry(
        "improve", conn, SETTINGS, _clock(), rag, activity=activity,
        staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {"who": "bt"},
                      "analyze_corr": lambda a: {"who": "ac"}})
    assert registry.func("run_backtest")(name="n", pair="USDJPY")["who"] == "bt"
    assert registry.func("analyze_corr")(request={})["who"] == "ac"


def test_build_mission_registry_improve_rejects_trade_only_kwargs(tmp_path):
    """M-4 (検収是正): improve 分岐は provider/readonly/indicator_plugins/
    sandbox_run (trade 専用の注入 seam) を無言で捨てていた — trade 分岐の
    `provider is not None and readonly` ガードより前に return するため、
    誤配線が検出されなかった。fail closed に倒す (ValueError)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir()
    source_snapshot_dir.mkdir()
    kwargs = dict(
        loop="improve", conn=conn, settings=SETTINGS, clock=_clock(), rag=rag,
        activity=activity, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})

    with pytest.raises(ValueError, match="provider/readonly"):
        build_mission_registry(**{**kwargs, "readonly": True})
    with pytest.raises(ValueError, match="provider/readonly"):
        build_mission_registry(**{**kwargs, "indicator_plugins": []})
    with pytest.raises(ValueError, match="provider/readonly"):
        build_mission_registry(**{**kwargs, "sandbox_run": lambda *a, **kw: None})
    with pytest.raises(ValueError, match="provider/readonly"):
        build_mission_registry(**{**kwargs, "provider": object()})  # L04


@pytest.mark.parametrize("missing_kwarg", [
    "staging_dir", "source_snapshot_dir", "ledger", "rpc_handlers"])
def test_build_mission_registry_improve_rejects_missing_required_kwarg(
        tmp_path, missing_kwarg):
    """L03: improve 分岐の 4 kwargs (staging_dir/source_snapshot_dir/
    ledger/rpc_handlers) の None ガードに個別テストが無かった。各 kwarg
    を単独で None にし、`ValueError` になることを確認する (ガードを
    消すと呼出時まで遅延して TypeError になる — fail-open)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir()
    source_snapshot_dir.mkdir()
    kwargs = dict(
        loop="improve", conn=conn, settings=SETTINGS, clock=_clock(), rag=rag,
        activity=activity, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})
    kwargs[missing_kwarg] = None
    with pytest.raises(ValueError,
                       match="staging_dir/source_snapshot_dir/ledger/"):
        build_mission_registry(**kwargs)


def test_build_mission_registry_trade_unaffected_by_improve_branch(tmp_path):
    """既存の trade 分岐が improve 分岐の追加で壊れていないことの回帰。"""
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=activity)
    assert "get_ohlcv" in registry.names()
    assert "web_search" not in registry.names()

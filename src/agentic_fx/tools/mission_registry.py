"""Mission ツール配線の単一の組み立て関数 (プラン 8 worker 基盤 — 設計書 §4.2)。

親 (build_app、起動時 _assert_tools_registered 検証) と子
(mission_worker.py、実行時) が**同一関数**を共有する — 配線の二重化を
防ぎ、「親で検証したものと子で動くものが同じ」を関数の同一性で担保する。

`provider` は呼び出し側の既存インスタンスを受け取る (注入 seam)。
非 None なら使用、None なら内部構築。`econ`/`broker` はこの関数の内部で
新規構築する (呼び出し側の既存インスタンスを受け取らない)。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.rag import Rag
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools,
    signal_tools, improve_rpc_tools, improve_staging_tools, research_tools,
)
from agentic_fx.tools.registry import ToolRegistry
from agentic_fx.tools.mission_counters import MissionToolCounters

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.plugin.loader import PluginMeta


def build_mission_registry(
        loop: str, conn: sqlite3.Connection, settings: "Settings",
        clock: Clock, rag: Rag, *, activity: ActivityLog,
        indicator_plugins: "list[PluginMeta] | None" = None,
        sandbox_run=None, readonly: bool = False,
        provider: PriceProvider | None = None,
        staging_dir: "Path | None" = None,
        source_snapshot_dir: "Path | None" = None,
        ledger: "ImproveRpcLedger | None" = None,
        rpc_handlers: "dict[str, Callable[[dict], dict]] | None" = None,
        ) -> ToolRegistry:
    """`loop == "improve"` は 7-E (プラン10 Task 7) で分岐するようになった
    (M-3, 検収是正 — 旧 docstring は「本プランでは分岐しない」としていたが
    実装と矛盾していた): improve 分岐は research/staging/rpc ツールだけの
    独立 registry を組み、trade 分岐 (`provider`/`indicator_plugins`/
    `sandbox_run`/`readonly` を使う既存経路) には落ちない。`loop` 以外の
    trade/ask 経路では、どのツールを実際に Mission に見せるかは呼び出し側の
    `Mission.tools` リスト (`_TRADE_TOOLS` 等) が決める (ここは無変更)。

    `readonly` (CR-4 対応、裁定書 F-5): 子プロセス (`mission_worker.py`)
    は `conn` に `db.connect_readonly` (SQLite `mode=ro`) を渡すため、
    `PriceProvider.get_bars`/`_derive` が通常経路で行う `ohlcv.upsert_cache_bars`
    キャッシュ書込は `sqlite3.OperationalError: attempt to write a
    readonly database` になる。`readonly=True` は `PriceProvider` を
    cache 書込スキップモードで構築する — 既存 cache は引き続き読むが、
    新規取得したバーの書込だけをスキップする。RPC 経由で親に書込を
    委譲する設計は採らない (設計裁定: RPC 面を拡大しない — 裁定書
    F-5)。cache は性能最適化であり、親の scheduler tick が継続的に
    cache を温めるため実害は限定的。

    scheduler tick の processed-bar marking が書き込み可能 provider 経由で
    1m cache を継続的に温める。1h は live 1h が検証を通れば直接保存され、
    通らない場合は保存済み 1m から cache(1m→1h derived) として復元される。
    readonly Mission provider はこの二段構えの書き手ではない。

    `provider` (注入 seam、build_app の `provider=` パラメータを透通する):
    非 None ならそれを使い、None なら `PriceProvider(conn, settings, clock,
    readonly=readonly)` で内部構築する。親 (build_app) からは常に非 None
    (実プロバイダまたはテスト注入プロバイダ) で渡され、子 (mission_worker.py)
    からは常に None で渡される (readonly=True で内部構築) 想定である。
    """
    if loop == "improve":
        # M-4 (検収是正): improve 分岐は trade 専用の注入 seam
        # (provider/readonly/indicator_plugins/sandbox_run) を使わない。
        # trade 分岐には `provider is not None and readonly` の誤配線
        # ガードがあるが、improve はそれより前に return するため、
        # 誤って trade 引数を伴って呼ばれても無言で捨てていた。fail closed
        # にする — 誤配線 (例: 子プロセスから trade 用引数のまま呼んだ) を
        # 検出可能にする。
        if provider is not None or readonly or indicator_plugins is not None \
                or sandbox_run is not None:
            raise ValueError(
                "loop='improve' は provider/readonly/indicator_plugins/"
                "sandbox_run を受け付けません (trade 専用の注入 seam)")
        if staging_dir is None or source_snapshot_dir is None or ledger is None \
                or rpc_handlers is None:
            raise ValueError(
                "loop='improve' には staging_dir/source_snapshot_dir/ledger/"
                "rpc_handlers が必須です")
        counters = MissionToolCounters()
        registry = ToolRegistry(on_execute=counters.record_call)
        registry.counters = counters
        budget = settings.improve.tool_budget
        registry.register_all(research_tools.build_research_tooldefs(
            settings=settings.improve.research))
        registry.register_all(improve_staging_tools.build_improve_staging_tooldefs(
            staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
            counters=counters, budget=budget))
        registry.register_all(improve_rpc_tools.build_improve_rpc_tooldefs(
            ledger=ledger, run_backtest_handler=rpc_handlers["run_backtest"],
            analyze_corr_handler=rpc_handlers["analyze_corr"],
            staging_dir=staging_dir, counters=counters, budget=budget))
        return registry

    # --- 既存 trade/ask 分岐 (無変更) ---
    # provider と readonly=True の併用は禁止: provider が指定されると readonly
    # は無視される。子プロセス (mission_worker.py) から呼ぶ場合は provider を
    # 渡さないこと。親は provider を渡す (readonly=False)、子は provider を渡さない
    # (readonly=True で内部構築)。
    if provider is not None and readonly:
        raise ValueError(
            "provider と readonly=True の併用は禁止: provider が指定されると "
            "readonly は無視される。子プロセスから呼ぶ場合は provider を渡さないこと。")

    if provider is None:
        provider = PriceProvider(conn, settings, clock, readonly=readonly)
    econ = EconCalendar(conn, activity, clock,
                        timeout_sec=settings.worker.data_hook_timeout_sec)
    broker = PaperBroker(conn, settings, clock)

    registry = ToolRegistry()
    registry.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=indicator_plugins,
        sandbox_run=sandbox_run))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn, broker))
    registry.register_all(reflection_tools.build(conn, rag, settings.pairs))
    registry.register_all(signal_tools.build(conn, settings, clock))
    return registry

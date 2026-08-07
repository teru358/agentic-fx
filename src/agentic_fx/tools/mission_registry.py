"""Mission ツール配線の単一の組み立て関数 (プラン 8 worker 基盤 — 設計書 §4.2)。

親 (build_app、起動時 _assert_tools_registered 検証) と子
(mission_worker.py、実行時) が**同一関数**を共有する — 配線の二重化を
防ぎ、「親で検証したものと子で動くものが同じ」を関数の同一性で担保する。

`provider`/`econ`/`broker` はこの関数の内部で新規構築する (呼び出し側の
既存インスタンスを受け取らない) — conn/settings/clock/activity から素直に
組み立てられる薄いラッパーであり、親の長寿命インスタンスと子の使い捨て
インスタンスを同じコードパスで作れることが本モジュールの目的そのもの。
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.rag import Rag
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools,
    signal_tools,
)
from agentic_fx.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.plugin.loader import PluginMeta


def build_mission_registry(
        loop: str, conn: sqlite3.Connection, settings: "Settings",
        clock: Clock, rag: Rag, *, activity: ActivityLog,
        indicator_plugins: "list[PluginMeta] | None" = None,
        sandbox_run=None, readonly: bool = False,
        provider: PriceProvider | None = None) -> ToolRegistry:
    """`loop` は本プランでは配線を分岐しない (常に同じ全ツール集合を
    構築する) — forward-compat 引数。どのツールを実際に Mission に
    見せるかは呼び出し側の `Mission.tools` リスト (`_TRADE_TOOLS` 等) が
    決める。将来の improve 系 registry 分岐 (プラン 9) で `loop` を
    使い始める想定。

    `readonly` (CR-4 対応、裁定書 F-5): 子プロセス (`mission_worker.py`)
    は `conn` に `db.connect_readonly` (SQLite `mode=ro`) を渡すため、
    `PriceProvider.get_bars`/`_derive` が通常経路で行う `ohlcv.upsert_bars`
    キャッシュ書込は `sqlite3.OperationalError: attempt to write a
    readonly database` になる。`readonly=True` は `PriceProvider` を
    cache 書込スキップモードで構築する — 既存 cache は引き続き読むが、
    新規取得したバーの書込だけをスキップする。RPC 経由で親に書込を
    委譲する設計は採らない (設計裁定: RPC 面を拡大しない — 裁定書
    F-5)。cache は性能最適化であり、親の scheduler tick が継続的に
    cache を温めるため実害は限定的。

    `provider` (注入 seam、build_app の `provider=` パラメータを透通する):
    非 None ならそれを使い、None なら `PriceProvider(conn, settings, clock,
    readonly=readonly)` で内部構築する。子プロセス (`mission_worker.py`)
    から呼ぶ場合は provider を渡さない — `readonly=True` で内部構築する。
    このパラメータはテスト注入専用であり、本番環境では常に None である。
    """
    if provider is None:
        provider = PriceProvider(conn, settings, clock, readonly=readonly)
    econ = EconCalendar(conn, activity, clock)
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

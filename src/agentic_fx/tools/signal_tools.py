"""get_signals ツール — 取引判断 loop 専用 (プラン 7 Task 9)。

承認済み signal/strategy plugin が signals テーブル (Task 7) に投入した
直近の出力を、取引判断 Mission から読めるようにする薄いラッパー。
`_TRADE_TOOLS` (loops/trade_loop.py) 経由で trade / ask 両 Mission に
露出する — read-only な判断材料であり、lookback 上限で遮断は保たれる
ため意図的 (opus R2 M11)。

**lookback のクランプ (二重防御)**: ToolDef の JSON Schema
(``minimum: 1`` / ``maximum: settings.plugin.signals_max_lookback_hours``)
は ``registry.execute`` (LLM 経由の呼び出し) 側の防御。関数本体でも同じ
範囲へクランプする — スキーマ検証を経由しない呼び出し (テスト・将来の
直接呼び出し) に対する防波堤。

**strategy 行への成績添付**: ``kind=="strategy"`` の行には
``backtest_runs.latest_in_sample_metrics(content_hash)`` を
``"in_sample_metrics"`` キーで添付し、``_ANNOTATION`` (バックテスト成績は
実運用成績の予測値ではない) を ``"note"`` キーで必ず同梱する (metrics が
None = 未計測でも注記だけは付ける — None であることが判断材料になる)。
signal 行には両キーとも付けない。

**改善ループ非露出**: ``IMPROVE_FORBIDDEN`` は改善ループ (プラン 9) の
allowed tools リストに現れてはならないツール名の集合。改善ループの
allowed リストの実体はまだ存在しない (現時点で改善ループの Mission 構築点
は ``loops/reflection_cycle.py`` の ``tools=[]`` のみ)。プラン 9 で実際の
allowed リストが実装されたら、その組み立て箇所で本集合との非交差を assert
すること (引き継ぎ)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta

from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock
from agentic_fx.store import backtest_runs, signals
from agentic_fx.tools import market_tools
from agentic_fx.tools.registry import ToolDef

_log = logging.getLogger("agentic_fx.tools.signal_tools")

# strategy 行の in_sample_metrics に必ず同梱する注記 (brief 逐語)。
_ANNOTATION = "バックテスト成績は実運用成績の予測値ではない"

# 改善ループの allowed tools リストに現れてはならないツール名の集合。
# "get_signals": 取引判断 loop 専用 (改善ループがバックテスト成績を材料に
# 使うのは run_in_sample/run_holdout_gate 経由であるべきで、この読み取り
# ツール経由ではない)。
# "bless": plugin 承認 (plugin/approval.py) は人間 CLI (`afx plugin bless`)
# 専用であり、そもそも ToolDef として存在しない — 改善ループの tools に
# 混入しうる経路自体が無いことの回帰ピンを兼ねる。
IMPROVE_FORBIDDEN = frozenset({"get_signals", "bless"})


def _clamp_since_hours(value: object, max_hours: int) -> int:
    """int 検証 (bool 除外) → ``[1, max_hours]`` へのクランプ。

    ``bool`` は ``int`` のサブクラスのため ``isinstance(value, int)`` だけ
    では ``True``/``False`` を通してしまう — 先に bool を弾く。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"since_hours must be an int (bool not allowed): {value!r}")
    return max(1, min(value, max_hours))


def build(conn: sqlite3.Connection, settings: Settings,
          clock: Clock) -> list[ToolDef]:
    """``get_signals(pair, since_hours=24)`` を提供する。

    brief は ``build(conn, settings)`` だが、``since_hours`` → ``since``
    (aware datetime) の変換に ``now()`` が要るため ``clock`` 引数へ拡張
    する (コントローラ解決)。service.py は既存の clock をそのまま渡す。
    """
    max_hours = settings.plugin.signals_max_lookback_hours

    def get_signals(pair: str, since_hours: int = 24) -> list[dict]:
        if pair not in settings.pairs:
            raise ValueError(
                f"pair must be one of {settings.pairs}: {pair!r}")
        clamped = _clamp_since_hours(since_hours, max_hours)
        since = clock.now() - timedelta(hours=clamped)
        rows = signals.recent(conn, pair, since=since)
        result: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                # fail-open: 1 行の payload 破損で get_signals 全体を
                # 失敗させない (LLM 向けの参考情報という位置づけは
                # get_indicators の plugin 失敗時の方針と同じ)。
                _log.warning(
                    "get_signals: payload_json decode failed for signal "
                    "id=%s — skipping", row["id"])
                continue
            item = {"id": row["id"], "plugin": row["plugin"],
                     "content_hash": row["content_hash"], "pair": row["pair"],
                     "timeframe": row["timeframe"], "bar_ts": row["bar_ts"],
                     "kind": row["kind"], "payload": payload}
            if row["kind"] == "strategy":
                item["in_sample_metrics"] = (
                    backtest_runs.latest_in_sample_metrics(
                        conn, row["content_hash"]))
                item["note"] = _ANNOTATION
            result.append(item)
        return result

    pair_schema = market_tools._pair_param(settings)["pair"]
    return [
        ToolDef(
            "get_signals",
            "取引判断 loop 専用: 承認済み signal/strategy plugin の直近 "
            "出力 (pair, 直近 since_hours 時間分・既定 24h)。strategy 行に"
            "は in_sample バックテスト成績 (in_sample_metrics) と、実運用"
            "成績の予測値ではない旨の注記 (note) が付く",
            {"type": "object",
             "properties": {
                 "pair": pair_schema,
                 "since_hours": {"type": "integer", "minimum": 1,
                                 "maximum": max_hours}},
             "required": ["pair"]},
            get_signals),
    ]

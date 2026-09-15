"""strategy 評価アダプタ — plugin (`evaluate`) を `IntentSource` へ変換する
(プラン 7 Task 5、設計書 §6)。

`IntentSource = Callable[[Bar], dict | None]` (backtest/runner.py) は
「評価 timeframe の確定バーを受け取り、LLM 出力と同形の intent dict (または
提案なし = None) を返す」契約。本モジュールはこれを strategy kind の
plugin (`evaluate(df, indicators, signals, params)`) で実装する。

**発火条件 (コントローラ裁定)**: `closed_bar.ts + eval_tf 幅` が plugin
宣言 timeframe のバケット境界 (epoch 錨、`timeframes.floor_to_bucket`) に
一致する tick のみ評価する。eval_tf 幅は `closed_bar.interval` から導出
する (`runner._aggregate_bucket` が `interval=eval_timeframe` を設定する
ため、アダプタ側に新引数を足さない — brief 明記)。`closed_bar.interval`
が未知の timeframe なら `ValueError` (fail closed)。発火格子に乗らない
tick は素通しで None を返す。

**幅導出 (レビュー fix round 1 F1 — codex Medium)**: 幅は
`timeframes.TF_MINUTES` ではなく `runner.parse_timeframe` を再利用して
導出する。`TF_MINUTES` は plugin 宣言 timeframe (`PLUGIN_TIMEFRAMES` —
15m/1h/4h/1d) 専用の列挙であり、`run_replay` が実際に受理する任意の
eval_timeframe (`_TF_RE` = 任意の `Nm`/`Nh`、例: 30m/90m/2h) を含まない。
`TF_MINUTES` で幅を引いていた旧実装は、CLI から到達可能な合法な
`--timeframe` (30m 等) で最初の評価が必ず `ValueError` になるバグを持って
いた。新しい写像テーブルは作らず、runner が `run_replay` 自身の駆動に
使っているパーサをそのまま import して使う (二重実装回避)。

**データ供給**: `load_resampled_frame(..., until=bucket_end,
max_bars=meta.max_bars)` — until 排他でも完成判定は
`bucket_end <= until` なので、ちょうど閉じた宣言 tf バケットは含まれる
(Task 0 の契約)。df が空なら評価せず None を返す (発火格子に乗っていても
データが無ければ評価しない — fail closed。source のタイプミスや履歴欠損を
「hold の連続」に読み替えてしまわないための判断)。

**セッション所有権 (コントローラ裁定)**: `session=None` (既定) では初回
発火時に `PluginSession` を lazy 生成して保持し、`close()` で閉じる —
バックテスト全体を通じて worker サブプロセス 1 個を使い回す
(`sandbox.py` の設計方針そのもの)。`session` が注入された場合、`close()`
はそれを閉じない (所有権は注入者にある — テストでの fake session 差し替え、
将来の producer 等での外部管理セッション共有を壊さないため)。

**SandboxError は捕まえず、呼び出し元へそのまま貫通させる** (fail closed —
plugin の実行時エラーは「評価そのものの失敗」であり、hold へ読み替えたり
run_replay を続行させたりしない)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Protocol

from agentic_fx.backtest.runner import parse_timeframe
from agentic_fx.backtest.timeframes import floor_to_bucket, load_resampled_frame
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import PluginSession

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet


class _SessionLike(Protocol):
    """`session` 注入シームが満たすべき最小契約 (`PluginSession.call` と
    同じ形)。fake session (テスト) はこれだけを実装すればよい — `close`/
    `__enter__` は「アダプタが lazy 生成する場合」にだけ要求される
    (`PluginSession` 自身が満たす)。"""

    def call(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class PluginStrategyIntentSource:
    """strategy plugin を 1 pair 分の `IntentSource` として振る舞わせる
    アダプタ (1 インスタンス = 1 pair — brief 明記)。`build_intent_source`
    経由で構築する。"""

    def __init__(self, meta: PluginMeta, *, conn: sqlite3.Connection,
                pair: str, dataset, settings: "Settings",
                resolved: "ResolvedIndicatorSet",
                session: _SessionLike | None = None,
                decision_sink: "Callable[[datetime, dict], None] | None" = None,
                ) -> None:
        """`resolved` は**必須** ([indicator-consumption-wiring] §2.3) —
        依存なしでも `ResolvedIndicatorSet.empty(inventory_root)` を渡す。
        アダプタは**内部で再解決しない**: 解決は composition root で 1 回、
        同じオブジェクトがここと `PluginSession` を通って worker まで届く。

        `decision_sink` は**テスト専用の観測面** (設計書 §6、codex r7 I2)。
        `backtest_runs` は metrics しか持たず `orders` は open しか表せない
        ため、hold を含む全時点の照合はこの sink でしか取れない。本番経路は
        常に `None` (既定) — 呼び出し元は渡さない。
        """
        # プラン 8 B 束 (Fable M-1): producer 側 (settings.pairs 外は
        # warning + skip) と対称の検証。adapter は 1 インスタンス = 1 pair
        # の明示的構築であり、meta.pairs に無い pair は「呼び出し側の
        # 取り違え」であって producer のように複数 pair を反復して一部
        # だけ諦める構造ではないため、即座に拒否する (fail closed)。
        if pair not in meta.pairs:
            raise ValueError(
                f"pair {pair!r} is not in plugin {meta.name!r}'s declared "
                f"pairs {meta.pairs!r}")
        self._meta = meta
        self._conn = conn
        self._pair = pair
        self._dataset = dataset
        self._settings = settings
        self._resolved = resolved
        self._session = session
        self._decision_sink = decision_sink
        # session を注入した呼び出し元がその所有者 (close の責務も持つ)。
        # 既定 (None) はこのアダプタ自身が lazy 生成し、自分で閉じる。
        self._owns_session = session is None
        self._cpu_sec: float | None = None
        # 観測性用カウンタ (brief 上書き節 6): 発火格子に乗り、かつ df が
        # 非空で実際に plugin を評価した回数。CLI はバックテスト完走後に
        # これが 0 なら「plugin が一度も発火しなかった」警告を出せる。
        self.eval_count = 0

    @property
    def cpu_sec(self) -> float | None:
        """自分が生成したセッションの累積 CPU 秒 (`close()` 後に確定)。
        注入セッション・未評価・異常終了では `None` (設計書 §2.4、C1)。"""
        return self._cpu_sec

    def __call__(self, closed_bar: Bar) -> dict | None:
        # F1 (レビュー fix round 1): TF_MINUTES ではなく runner の
        # parse_timeframe を再利用する (docstring 参照)。パース不能な
        # interval は parse_timeframe 自身が ValueError を送出する
        # (fail closed — 独自メッセージへの包み直しはしない)。
        width = parse_timeframe(closed_bar.interval)
        bucket_end = closed_bar.ts + width
        if floor_to_bucket(bucket_end, self._meta.timeframe) != bucket_end:
            return None  # plugin 宣言 timeframe の境界に乗っていない tick

        df = load_resampled_frame(
            self._conn, self._pair, self._meta.timeframe,
            source=self._dataset.source,
            base_interval=self._dataset.base_interval,
            until=bucket_end, max_bars=self._meta.max_bars)
        if df.empty:
            return None  # 発火格子に乗っていてもデータが無ければ評価しない

        session = self._ensure_session()
        self.eval_count += 1
        result = session.call({"df": df, "params": self._meta.params})
        if self._decision_sink is not None:
            self._decision_sink(bucket_end, result)
        return _strategy_result_to_intent(result, self._pair)

    def _ensure_session(self) -> _SessionLike:
        if self._session is None:
            session = PluginSession(self._meta, settings=self._settings.plugin,
                                    resolved=self._resolved)
            session.__enter__()
            self._session = session
        return self._session

    def close(self) -> None:
        """自分が生成したセッションのみ閉じる (注入されたセッションは
        呼び出し元の所有物 — close しない。コントローラ裁定)。close 完了後
        に `cpu_sec` を取り込む (`PluginSession.cpu_sec` は close 後に確定
        する property)。"""
        if self._owns_session and self._session is not None:
            self._session.close()
            self._cpu_sec = getattr(self._session, "cpu_sec", None)
            self._session = None


def build_intent_source(meta: PluginMeta, *, conn: sqlite3.Connection,
                        pair: str, dataset, settings: "Settings",
                        resolved: "ResolvedIndicatorSet",
                        session: _SessionLike | None = None,
                        decision_sink: "Callable[[datetime, dict], None] | None" = None,
                        ) -> PluginStrategyIntentSource:
    """`meta` (kind="strategy") から `pair` 用の `IntentSource` を組み立てる。

    呼び出し元 (CLI 等) は `run_replay` 実行後、必ず `close()` を呼ぶこと
    (try/finally — brief 明記。サンドボックスプロセスのリーク防止)。
    `resolved` は必須 — 依存なしでも空 set を渡す。"""
    return PluginStrategyIntentSource(
        meta, conn=conn, pair=pair, dataset=dataset, settings=settings,
        resolved=resolved, session=session, decision_sink=decision_sink)


def _strategy_result_to_intent(result: dict[str, Any], pair: str) -> dict | None:
    """`sandbox._validate_strategy_result` が返す検証済み dict
    (`{"action", "rationale", "direction", "entry_type", "limit_price",
    "stop_loss", "take_profit"}`) を LLM 出力と同形の intent dict へ写像
    する。action="hold" は None。

    `take_profit`: `None` なら省略する (コントローラ裁定 —
    `TradeIntent.from_llm_dict` は `d.get("take_profit")` で読むため、
    キー自体を省略しても値 `None` を明示的に含めても同値。省略側を選ぶ)。
    `limit_price`/`expires_in` は `entry_type == "limit"` のときのみ含める
    (brief 逐語)。`expires_in` は固定 "4h"。`confidence` は固定 0.5
    (brief 逐語 — plugin は確信度を返さない語彙のため)。
    """
    if result["action"] == "hold":
        return None

    intent: dict[str, Any] = {
        "action": "open",
        "pair": pair,
        "direction": result["direction"],
        "entry_type": result["entry_type"],
        "horizon": "day",
        "stop_loss": result["stop_loss"],
        "confidence": 0.5,
        "reasoning": result["rationale"],
    }
    if result["take_profit"] is not None:
        intent["take_profit"] = result["take_profit"]
    if result["entry_type"] == "limit":
        intent["limit_price"] = result["limit_price"]
        intent["expires_in"] = "4h"
    return intent

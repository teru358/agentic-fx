"""シグナル生産 — 承認済み signal/strategy plugin を評価し signals キューへ
詰める (プラン 7 Task 8、設計書 §6)。

**バケット進行検出 (opus R2 C2)**: 本番の ``now`` は非分格子 (秒・マイクロ
秒を含む) なので「今が丁度バケット境界」という一致判定は永久に成立しない。
代わりに「前回評価したバケットより後、かつ現在のバケット (未確定なので
含まない) より前に開始する確定バケット」を評価対象とする — 各 tick で
``floor = floor_to_bucket(now, meta.timeframe)`` を計算し、``cursor < b <
floor`` かつ ``b >= now − tf幅 × signal_freshness_bars`` を満たすバケット
開始時刻 ``b`` を古い順に評価する。

**評価 cursor はメモリ辞書のみ** (``(plugin.name, content_hash, pair)`` →
評価成功済みバケット開始時刻。テーブル追加なし)。この規則が 3 つの障害を
同時に有界回復する (codex R3 I1):

1. **再起動 (cursor 喪失)**: 鮮度窓内の確定バケットを再評価する。hold
   だったバケットも再評価されるが、signals の UNIQUE dedupe が重複挿入を
   防ぎ、再評価コストも窓幅で有界。
2. **複数バケット停止**: 窓を超えた分は評価しない (鮮度ゲートと同じ裁定
   — 古い判断を今さら起こさない)。窓外の古いバケットは評価せずそのまま
   cursor を進める (自然放棄)。
3. **plugin 失敗**: sandbox 呼び出しが失敗したバケットで catch-up を打ち
   切る (cursor をそのバケットの手前で止める) — 次 tick で同じバケットを
   再試行する。同じ理由でデータ欠損 (df 空) も同じ扱いにする — 「本番
   source にまだ取り込まれていないだけ」を fail-open で捌く。

**source** は本番運用の data source (``settings.plugin.producer_source``、
既定 "yfinance") — 承認バックテスト (Task 6) の eval source とは意図的に
異なる (承認 payload の "live_source" に差異を記載済み)。

**評価対象**: 承認済み signal/strategy plugin × 宣言 pairs ∩
settings.pairs (交差の外は warning でスキップ)。signal の ``detect`` は
各出力を ``kind="signal"`` の行にする。strategy の ``evaluate`` は
``action="open"`` の結果だけを ``kind="strategy"`` の行にする
(``"hold"`` は保存しない)。行の ``bar_ts`` は評価対象バケットの開始時刻を
ハーネス (このモジュール) が設定する — plugin 出力からは受け取らない
(sandbox.py の signal 経路は ``bar_ts`` キーそのものを reject する)。

**セッション償却**: ``sandbox_run`` 省略時 (既定 None) は plugin ごとに
``PluginSession`` を 1 個だけ開き、``evaluate_due_plugins`` 呼び出し全体
(複数 pair・複数バケットの catch-up を含む) で使い回す — 1 回の producer
tick で plugin×pair×bucket の数だけ worker サブプロセスを起動しては
性能が成立しない (sandbox.py 自身が明記する設計方針と同じ)。
``sandbox_run`` が注入された場合 (テスト) はセッション管理をバイパスし、
``market_tools.build``/``signal_eval.py`` と同じ呼び出し規約
``sandbox_run(meta, payload, settings=settings.plugin)`` で毎回呼ぶ。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from agentic_fx.backtest.timeframes import TF_MINUTES, floor_to_bucket, load_resampled_frame
from agentic_fx.plugin import sandbox as plugin_sandbox
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.store import signals

if TYPE_CHECKING:
    import sqlite3

    from agentic_fx.config import Settings

_log = logging.getLogger("agentic_fx.plugin.signal_producer")

# market_tools.build / signal_eval.py と同じ注入シグネチャ規約:
# (meta, payload, *, settings) -> dict。
SandboxRunFn = Callable[..., dict]

# 評価失敗 (sandbox 例外・データ欠損) を catch-up ループ内で統一的に扱う
# ための内部呼び出し規約: (meta, payload) -> dict。
_CallFn = Callable[[PluginMeta, dict], dict]


class SignalProducer:
    """評価 cursor (メモリのみ) を保持する signal/strategy plugin 評価器。

    1 インスタンス = 1 cursor 集合。service が再起動して producer を作り
    直せば cursor は失われ、鮮度窓内の確定バケットが catch-up 再評価される
    (冪等 — signals の UNIQUE dedupe が重複挿入を防ぐ)。
    """

    def __init__(self) -> None:
        # (plugin.name, content_hash, pair) -> 最後に評価成功したバケット開始時刻
        self._cursor: dict[tuple[str, str, str], datetime] = {}

    def evaluate_due_plugins(self, conn: "sqlite3.Connection", *,
                             plugins: list[PluginMeta], now: datetime,
                             source: str, sandbox_run: SandboxRunFn | None = None,
                             settings: "Settings") -> int:
        """承認済み signal/strategy plugin のうち評価期限が来たバケットを
        評価し、新規挿入した signals 行の総数を返す。

        ``plugins`` は ``plugin_loader.approved_plugins()`` の戻り値
        (承認済み・任意 kind 混在) をそのまま渡してよい — kind が
        signal/strategy 以外の要素はここで無視する。
        """
        inserted = 0
        sessions: dict[str, plugin_sandbox.PluginSession] = {}

        def _call(meta: PluginMeta, payload: dict) -> dict:
            if sandbox_run is not None:
                return sandbox_run(meta, payload, settings=settings.plugin)
            session = sessions.get(meta.content_hash)
            if session is None:
                session = plugin_sandbox.PluginSession(meta, settings=settings.plugin)
                session.__enter__()
                sessions[meta.content_hash] = session
            return session.call(payload)

        try:
            for meta in plugins:
                if meta.kind not in ("signal", "strategy"):
                    continue
                for pair in meta.pairs:
                    if pair not in settings.pairs:
                        _log.warning(
                            "plugin %s: pair %s is not in settings.pairs — "
                            "skipping", meta.name, pair)
                        continue
                    inserted += self._evaluate_one(
                        conn, meta, pair, now=now, source=source, call=_call,
                        settings=settings)
        finally:
            for session in sessions.values():
                session.close()
        return inserted

    def _evaluate_one(self, conn: "sqlite3.Connection", meta: PluginMeta,
                      pair: str, *, now: datetime, source: str,
                      call: _CallFn, settings: "Settings") -> int:
        """1 plugin × 1 pair 分の catch-up 評価。古いバケットから順に評価し、
        失敗したバケットの手前で打ち切る (cursor はそこまでしか進めない)。
        """
        tf = meta.timeframe
        width = timedelta(minutes=TF_MINUTES[tf])
        floor = floor_to_bucket(now, tf)
        freshness_cutoff = now - width * settings.plugin.signal_freshness_bars
        key = (meta.name, meta.content_hash, pair)
        cursor = self._cursor.get(key)
        start = (cursor + width if cursor is not None
                 else floor_to_bucket(freshness_cutoff, tf))

        inserted = 0
        b = start
        while b < floor:
            if b < freshness_cutoff:
                # 窓外の古いバケット — 評価せず自然放棄 (cursor だけ進める)
                self._cursor[key] = b
                b += width
                continue
            try:
                inserted += self._evaluate_bucket(
                    conn, meta, pair, b, now=now, width=width, source=source,
                    call=call)
            except Exception as e:  # noqa: BLE001 — plugin 単位で fail-open
                _log.warning(
                    "plugin %s (%s): evaluation failed at bucket %s (%s) — "
                    "cursor not advanced, retry next tick",
                    meta.name, pair, b.isoformat(), e)
                break
            self._cursor[key] = b
            b += width
        return inserted

    def _evaluate_bucket(self, conn: "sqlite3.Connection", meta: PluginMeta,
                         pair: str, bucket_start: datetime, *, now: datetime,
                         width: timedelta, source: str, call: _CallFn) -> int:
        bucket_end = bucket_start + width
        df = load_resampled_frame(
            conn, pair, meta.timeframe, source=source, until=bucket_end,
            max_bars=meta.max_bars)
        # fix round 1 F3 (codex): df が非空でも「末尾行 = 対象バケット」と
        # は限らない — 取り込みラグ/欠損で対象バケット分の 1m 行が 1 本も
        # 無い場合、resample はそのバケットの行を生成せず、df の末尾は
        # それより古い (既に評価済みの) バケットのままになる。この状態を
        # 「評価完了」として扱い bar_ts=bucket_start の signal を偽造して
        # cursor を進めてしまうと、後から本物のバーが取り込まれても二度と
        # 再評価されない (cursor はバケット単位の単調増加のみ)。空 df と
        # 同じ扱い (fail-open・cursor を進めず次 tick 再試行) にする。
        if df.empty or df.index[-1] != bucket_start:
            raise ValueError(
                f"target bucket not yet present in {source} data for "
                f"{pair} {meta.timeframe} bucket starting "
                f"{bucket_start.isoformat()}")

        # `load_resampled_frame` は「在る分だけ」を返すので、要求 max_bars に
        # 満たない窓でも上の fail-open 分岐には入らない (末尾バケットは
        # 存在する)。切り詰められた系列で指標が計算され signal がそのまま
        # 出るため、無音のまま品質が落ちる。承認時は削除されない履歴
        # テーブルで評価するので、この劣化は本番でしか現れない。窓が
        # 足りない主因はキャッシュ保持期間なので、設定名を出して知らせる。
        if len(df) < meta.max_bars:
            _log.warning(
                "plugin %s (%s): %s の窓が %d 本しか読めなかった "
                "(max_bars=%d 要求) — datafeed.cache_retention_days が "
                "plugin の要求窓に対して短い可能性がある。指標は切り詰め"
                "られた系列で計算される",
                meta.name, pair, meta.timeframe, len(df), meta.max_bars)

        bar_ts = bucket_start.isoformat()
        if meta.kind == "signal":
            result = call(meta, {"df": df, "params": meta.params})
            inserted = 0
            for i, sig in enumerate(result["signals"]):
                row_id = signals.add(
                    conn, plugin=meta.name, content_hash=meta.content_hash,
                    pair=pair, timeframe=meta.timeframe, bar_ts=bar_ts,
                    kind="signal", payload=sig, now=now)
                if row_id is not None:
                    inserted += 1
                elif i > 0:
                    # fix round 1 F4 (codex): UNIQUE (plugin, content_hash,
                    # pair, timeframe, bar_ts) は同一バケットで 1 出力しか
                    # 保持できない — DDL は §12 逐語のため変更しない
                    # (plan 由来の構造的制約として最終レビューに送る)。
                    # 2 出力目以降が dedupe で音もなく消えることだけは
                    # observability を確保する (i==0 は「本物の重複」との
                    # 区別がつかないため対象外)。
                    _log.warning(
                        "plugin %s (%s): signal[%d] at bar_ts=%s was "
                        "dropped by the (plugin, content_hash, pair, "
                        "timeframe, bar_ts) UNIQUE constraint — only the "
                        "first signal per bucket is stored",
                        meta.name, pair, i, bar_ts)
            return inserted

        # strategy: action="open" のみ保存 ("hold" は非保存)
        result = call(meta, {"df": df, "indicators": None, "signals": None,
                              "params": meta.params})
        if result.get("action") != "open":
            return 0
        row_id = signals.add(
            conn, plugin=meta.name, content_hash=meta.content_hash,
            pair=pair, timeframe=meta.timeframe, bar_ts=bar_ts,
            kind="strategy", payload=result, now=now)
        return 1 if row_id is not None else 0

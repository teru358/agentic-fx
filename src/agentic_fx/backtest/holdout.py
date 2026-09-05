"""holdout — in-sample / holdout gate 分割 (プラン 6 Task 9)。

holdout の遮断はこのシステムの根幹: 改善ループ (プラン 9) には in-sample
しか見せない。期間・端点はこのモジュールが所有し、``run_in_sample`` /
``run_holdout_gate`` はどちらも期間引数を取らず、返り値は
``compute_metrics.METRIC_KEYS`` の 8 キーのみ (期間・端点は返さない —
§6 遮断 1)。

``run_holdout_gate`` の呼び出し元は **採用ゲート (人間承認フロー) に限る**。
本モジュールでの防御は API 形状 (issuer 固定・改善ループ tool に載せない)
まで — 到達不能性の構造的成立 (エージェント worker からの import/実行遮断)
はプラン 8 の worker 権限境界が担い、プラン 9 の blocking 受入条件で統合
検証する (レビュー裁定 codex C1, task-9-brief.md)。改善ループ (プラン 9)
の tool 定義に ``run_holdout_gate`` を絶対に載せないこと。

期間の実行は Task 7 の ``run_replay`` (実運用と同一コアで再生)、成績集計は
Task 8 の ``compute_metrics``、永続化は ``backtest_runs.save_harness_run``
(呼び出し元をこのモジュールの 2 関数だけに限定する規約 — Task 8 申し送り)
を使う。``run_replay``/``compute_metrics`` はモジュール属性として直接
参照する (テストの monkeypatch 対象・配線ピン用 — 上書き節 A)。
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.metrics import compute_metrics
from agentic_fx.backtest.runner import IntentSource, run_replay
from agentic_fx.config import Settings
from agentic_fx.store.backtest_runs import (
    core_commit, save_harness_run, settings_snapshot_hash,
)

_UTC = timezone.utc
_log = logging.getLogger(__name__)


class NoHistoryError(ValueError):
    """Requested symbol/source has no local 1m history."""


def _require_aware(dt: datetime, label: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{label} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(_UTC)


def holdout_boundary(now: datetime, months: int) -> datetime:
    """``now`` から暦月で ``months`` 遡った UTC 時刻。

    日はクランプ (対象月の末日を超える場合は末日にする)。時刻部分は保持。
    純関数 — dateutil は使わず「月を引き、日をクランプ」の素朴実装
    (task-9-brief.md 上書き節 B)。
    """
    if months < 1:
        raise ValueError(f"months must be >= 1: {months!r}")
    now_utc = _require_aware(now, "now")
    total_month_index = now_utc.year * 12 + (now_utc.month - 1) - months
    year, month0 = divmod(total_month_index, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(now_utc.day, last_day)
    return now_utc.replace(year=year, month=month, day=day)


def _normalize_now(now: datetime) -> datetime:
    """UTC へ正規化し、分格子へ切り捨てる (run_replay の格子契約)。"""
    now_utc = _require_aware(now, "now")
    return now_utc.replace(second=0, microsecond=0)


def in_sample_until(now: datetime, months: int) -> datetime:
    """in-sample 境界算術の単一所有者 (F1, 最終レビュー opus I-1 是正)。

    ``holdout_boundary`` は h/m/s/µs を保持したまま暦月を引くだけなので、
    呼び出し元が ``now`` を分格子へ切り捨てずに渡すと境界が呼び出し元ごと
    に最大 1 分弱ずれる (``run_in_sample``/``run_holdout_gate`` は
    ``_normalize_now`` 経由、``analyze_for_agent`` は素の ``as_utc`` 経由
    だったため不一致が生じていた)。in-sample 境界を必要とする全ての
    呼び出し元は本関数だけを経由すること — ``holdout_boundary`` を直接
    呼ばない。
    """
    return holdout_boundary(_normalize_now(now), months)


def _oldest_bar_start(history_conn: sqlite3.Connection, symbol: str,
                      source: str) -> datetime:
    row = history_conn.execute(
        "SELECT MIN(bar_time) FROM ohlcv_history WHERE symbol=? AND interval='1m' "
        "AND source=?", (symbol, source)).fetchone()
    bar_time_iso = row[0] if row is not None else None
    if bar_time_iso is None:
        raise NoHistoryError(
            f"no 1m history for symbol={symbol!r} source={source!r} "
            "(cannot determine in-sample start)")
    start = datetime.fromisoformat(bar_time_iso).astimezone(_UTC)
    # F2 (fix round 1, codex Important + sonnet Important-3): ohlcv.
    # import_history_bars/_validate_and_normalize_row は bar_time の分格子
    # (second==microsecond==0) を検証しない — 別経路のインポータが秒付き
    # タイムスタンプを書き込むと、そのまま run_replay/ReplayClock に渡って
    # しまう。本番なら ReplayClock 構築時に確実に ValueError で落ちるが
    # (fail closed)、原因が分かりにくいままそこまで到達させず、ここで
    # 明示的に検出する。メッセージにタイムスタンプは含めない (F1 と同じ
    # 遮断規律 — 期間・端点の漏洩は例外経路にも適用される)。
    if start.second != 0 or start.microsecond != 0:
        raise ValueError(
            f"oldest 1m bar for symbol={symbol!r} source={source!r} is not "
            "on minute boundary (second and microsecond must be 0 — data "
            "corruption?)")
    return start


def _run_scope(settings: Settings, *, scope: str,
              history_conn: sqlite3.Connection, symbol: str, dataset: HistoryDataset,
              intent_source: IntentSource, eval_timeframe: str,
              plugin_ref: str, content_hash: str, kind: str, now: datetime,
              period_start: datetime, period_end: datetime,
              record_fn: "Callable[[dict], int] | None" = None) -> dict:
    result = run_replay(
        settings, symbol=symbol, dataset=dataset, start=period_start,
        end=period_end, intent_source=intent_source,
        eval_timeframe=eval_timeframe, history_conn=history_conn)
    metrics = compute_metrics(result)
    save_kwargs = dict(
        scope=scope, plugin_ref=plugin_ref, content_hash=content_hash,
        kind=kind, pair=symbol, timeframe=eval_timeframe, source=dataset.source,
        base_interval=dataset.base_interval, params={},
        period=(period_start, period_end), metrics=metrics,
        settings_hash=settings_snapshot_hash(settings),
        core_commit=core_commit(),
        initial_balance=settings.backtest.initial_balance, now=now)
    if record_fn is not None:
        record_fn(save_kwargs)
    else:
        save_harness_run(history_conn, **save_kwargs)
    return dict(metrics)


def run_in_sample(settings: Settings, *, history_conn: sqlite3.Connection,
                  symbol: str, dataset: HistoryDataset, intent_source: IntentSource,
                  eval_timeframe: str, plugin_ref: str, content_hash: str,
                  kind: str, now: datetime,
                  record_fn: "Callable[[dict], int] | None" = None) -> dict:
    """in-sample 期間 (source の最古バー, ``holdout_boundary(now)``) を再生し、
    ``backtest_runs.save_harness_run(scope="in_sample")`` に保存する。

    期間引数を取らない — 期間・端点はこのハーネスが所有し、返り値にも
    含めない (§6 遮断 1)。months は ``settings.backtest.holdout_months``
    経由のみ (呼び出し側が境界を動かせる自由度を作らない)。
    """
    now_norm = _normalize_now(now)
    boundary = in_sample_until(now_norm, settings.backtest.holdout_months)
    start = _oldest_bar_start(history_conn, symbol, dataset.source)
    if start >= boundary:
        # F1 (fix round 1, codex Important): 遮断 1 (期間・端点はハーネスが
        # 所有) は例外経路にも適用される — 改善ループが例外本文を観測でき
        # る構成だと boundary の完全な時刻が漏れるため、例外メッセージには
        # タイムスタンプを含めない。詳細は運用ログ (改善ループから隔離
        # された側) にのみ出す。
        _log.warning(
            "in-sample period is empty for symbol=%r source=%r: oldest bar "
            "%s >= holdout boundary %s", symbol, dataset.source,
            start.isoformat(), boundary.isoformat())
        raise ValueError(
            "in-sample period is empty (oldest bar >= holdout boundary)")
    return _run_scope(
        settings, scope="in_sample", history_conn=history_conn,
        symbol=symbol, dataset=dataset, intent_source=intent_source,
        eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
        content_hash=content_hash, kind=kind, now=now_norm,
        period_start=start, period_end=boundary, record_fn=record_fn)


def run_holdout_gate(settings: Settings, *, history_conn: sqlite3.Connection,
                     symbol: str, dataset: HistoryDataset, intent_source: IntentSource,
                     eval_timeframe: str, plugin_ref: str, content_hash: str,
                     kind: str, now: datetime,
                     record_fn: "Callable[[dict], int] | None" = None) -> dict:
    """holdout 期間 (``holdout_boundary(now)``, now) を再生し、
    ``backtest_runs.save_harness_run(scope="holdout_gate")`` に保存する。

    **呼び出し元は採用ゲート (人間承認フロー) に限る。** 改善ループ
    (プラン 9) の tool 定義には絶対に載せないこと — 本モジュールでの防御は
    API 形状までで、到達不能性の構造的成立はプラン 8/9 が担う
    (レビュー裁定 codex C1)。
    """
    now_norm = _normalize_now(now)
    boundary = in_sample_until(now_norm, settings.backtest.holdout_months)
    return _run_scope(
        settings, scope="holdout_gate", history_conn=history_conn,
        symbol=symbol, dataset=dataset, intent_source=intent_source,
        eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
        content_hash=content_hash, kind=kind, now=now_norm,
        period_start=boundary, period_end=now_norm, record_fn=record_fn)

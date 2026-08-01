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
import sqlite3
from datetime import datetime, timezone

from agentic_fx.backtest.metrics import compute_metrics
from agentic_fx.backtest.runner import IntentSource, run_replay
from agentic_fx.config import Settings
from agentic_fx.store.backtest_runs import (
    core_commit, save_harness_run, settings_snapshot_hash,
)

_UTC = timezone.utc


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


def _oldest_bar_start(history_conn: sqlite3.Connection, symbol: str,
                      source: str) -> datetime:
    row = history_conn.execute(
        "SELECT MIN(bar_time) FROM ohlcv WHERE symbol=? AND interval='1m' "
        "AND source=?", (symbol, source)).fetchone()
    bar_time_iso = row[0] if row is not None else None
    if bar_time_iso is None:
        raise ValueError(
            f"no 1m history for symbol={symbol!r} source={source!r} "
            "(cannot determine in-sample start)")
    return datetime.fromisoformat(bar_time_iso).astimezone(_UTC)


def _run_scope(settings: Settings, *, scope: str,
              history_conn: sqlite3.Connection, symbol: str, source: str,
              intent_source: IntentSource, eval_timeframe: str,
              plugin_ref: str, content_hash: str, kind: str, now: datetime,
              period_start: datetime, period_end: datetime) -> dict:
    result = run_replay(
        settings, symbol=symbol, source=source, start=period_start,
        end=period_end, intent_source=intent_source,
        eval_timeframe=eval_timeframe, history_conn=history_conn)
    metrics = compute_metrics(result)
    save_harness_run(
        history_conn, scope=scope, plugin_ref=plugin_ref,
        content_hash=content_hash, kind=kind, pair=symbol,
        timeframe=eval_timeframe, source=source,
        period=(period_start, period_end), metrics=metrics,
        settings_hash=settings_snapshot_hash(settings),
        core_commit=core_commit(),
        initial_balance=settings.backtest.initial_balance, now=now)
    return dict(metrics)


def run_in_sample(settings: Settings, *, history_conn: sqlite3.Connection,
                  symbol: str, source: str, intent_source: IntentSource,
                  eval_timeframe: str, plugin_ref: str, content_hash: str,
                  kind: str, now: datetime) -> dict:
    """in-sample 期間 (source の最古バー, ``holdout_boundary(now)``) を再生し、
    ``backtest_runs.save_harness_run(scope="in_sample")`` に保存する。

    期間引数を取らない — 期間・端点はこのハーネスが所有し、返り値にも
    含めない (§6 遮断 1)。months は ``settings.backtest.holdout_months``
    経由のみ (呼び出し側が境界を動かせる自由度を作らない)。
    """
    now_norm = _normalize_now(now)
    boundary = holdout_boundary(now_norm, settings.backtest.holdout_months)
    start = _oldest_bar_start(history_conn, symbol, source)
    if start >= boundary:
        raise ValueError(
            "in-sample period is empty: oldest bar "
            f"({start.isoformat()}) >= holdout boundary "
            f"({boundary.isoformat()})")
    return _run_scope(
        settings, scope="in_sample", history_conn=history_conn,
        symbol=symbol, source=source, intent_source=intent_source,
        eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
        content_hash=content_hash, kind=kind, now=now_norm,
        period_start=start, period_end=boundary)


def run_holdout_gate(settings: Settings, *, history_conn: sqlite3.Connection,
                     symbol: str, source: str, intent_source: IntentSource,
                     eval_timeframe: str, plugin_ref: str, content_hash: str,
                     kind: str, now: datetime) -> dict:
    """holdout 期間 (``holdout_boundary(now)``, now) を再生し、
    ``backtest_runs.save_harness_run(scope="holdout_gate")`` に保存する。

    **呼び出し元は採用ゲート (人間承認フロー) に限る。** 改善ループ
    (プラン 9) の tool 定義には絶対に載せないこと — 本モジュールでの防御は
    API 形状までで、到達不能性の構造的成立はプラン 8/9 が担う
    (レビュー裁定 codex C1)。
    """
    now_norm = _normalize_now(now)
    boundary = holdout_boundary(now_norm, settings.backtest.holdout_months)
    return _run_scope(
        settings, scope="holdout_gate", history_conn=history_conn,
        symbol=symbol, source=source, intent_source=intent_source,
        eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
        content_hash=content_hash, kind=kind, now=now_norm,
        period_start=boundary, period_end=now_norm)

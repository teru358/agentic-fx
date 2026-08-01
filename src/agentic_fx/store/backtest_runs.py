"""backtest_runs — バックテスト成績の永続化 (プラン 6 Task 8)。

発行主体の分離 (レビュー裁定 codex C2): 公開面は 2 つだけ。
``save_harness_run`` (scope は in_sample/holdout_gate のみ、issued_by は
'harness' 固定・**呼び出し元は Task 9 の run_in_sample/run_holdout_gate
だけ**) と ``save_human_run`` (scope パラメータを持たず 'human_custom' /
issued_by='human_cli' 固定、CLI 用)。issuer を呼び出し引数として公開
しないことで、任意コードが in_sample を偽装保存する経路をモジュール境界
で塞ぐ (完全な強制はプラン 8 の worker 権限境界 — ここは API 形状での
防御 + DB CHECK 制約の二重)。

``in_sample_view`` は改善ループ (プラン 9) が読む唯一の面。
``scope='in_sample' AND issued_by='harness'`` に絞り、``period_start`` /
``period_end`` は返却列に含めない (holdout 遮断 1)。

fix round 1 F1 (codex Important): metrics dict は save_harness_run の
任意入力なので、"period_start"/"period_end" 等を metrics に混入されると
列遮断を metrics_json 経由で密輸できてしまう。``in_sample_view`` は
``agentic_fx.backtest.metrics.METRIC_KEYS`` で読み側の白リスト濾過を行う
(このモジュールは store 配下だが backtest.metrics に依存する — 依存の
向きが逆転しているように見えるが、遮断境界を「metrics 返却キーの定義元」
に一元化するための意図的な選択。import 方向は store-first/backtest-first
どちらの起動順でも循環しないことを確認済み)。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any

from agentic_fx.backtest.metrics import METRIC_KEYS

_HARNESS_SCOPES = frozenset({"in_sample", "holdout_gate"})

_VIEW_COLUMNS = (
    "id, plugin_ref, content_hash, kind, pair, timeframe, source, scope, "
    "issued_by, metrics_json, settings_hash, core_commit, initial_balance, "
    "created_at"
)


def _require_utc(dt: datetime, what: str) -> datetime:
    """tz-aware datetime を UTC に正規化する (naive は fail closed)。

    プロジェクト方針 (store/rag.py の ``_require_utc`` と同じ規律):
    「naive を UTC とみなす」処理は禁止。
    """
    if dt.tzinfo is None:
        raise ValueError(f"{what} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


def _insert(conn: sqlite3.Connection, *, scope: str, issued_by: str,
            plugin_ref: str, content_hash: str, kind: str, pair: str,
            timeframe: str, source: str, period: tuple[datetime, datetime],
            metrics: dict, settings_hash: str, core_commit: str,
            initial_balance: float, now: datetime) -> int:
    start, end = period
    start_utc = _require_utc(start, "period[0]")
    end_utc = _require_utc(end, "period[1]")
    now_utc = _require_utc(now, "now")
    metrics_json = json.dumps(metrics, sort_keys=True)
    cur = conn.execute(
        "INSERT INTO backtest_runs (plugin_ref, content_hash, kind, pair, "
        "timeframe, source, period_start, period_end, scope, issued_by, "
        "metrics_json, settings_hash, core_commit, initial_balance, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plugin_ref, content_hash, kind, pair, timeframe, source,
         start_utc.isoformat(), end_utc.isoformat(), scope, issued_by,
         metrics_json, settings_hash, core_commit, initial_balance,
         now_utc.isoformat()))
    conn.commit()
    return cur.lastrowid


def save_harness_run(conn: sqlite3.Connection, *, scope: str, plugin_ref: str,
                      content_hash: str, kind: str, pair: str, timeframe: str,
                      source: str, period: tuple[datetime, datetime],
                      metrics: dict, settings_hash: str, core_commit: str,
                      initial_balance: float, now: datetime) -> int:
    """ハーネス発行 (issued_by='harness' 固定)。scope は in_sample/holdout_gate のみ。

    呼び出し元は Task 9 の run_in_sample / run_holdout_gate だけ (規約 —
    このモジュールは呼び出し元を強制しない、API 形状での防御に留まる)。
    """
    if scope not in _HARNESS_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_HARNESS_SCOPES)}: {scope!r}")
    return _insert(
        conn, scope=scope, issued_by="harness", plugin_ref=plugin_ref,
        content_hash=content_hash, kind=kind, pair=pair, timeframe=timeframe,
        source=source, period=period, metrics=metrics,
        settings_hash=settings_hash, core_commit=core_commit,
        initial_balance=initial_balance, now=now)


def save_human_run(conn: sqlite3.Connection, *, plugin_ref: str,
                    content_hash: str, kind: str, pair: str, timeframe: str,
                    source: str, period: tuple[datetime, datetime],
                    metrics: dict, settings_hash: str, core_commit: str,
                    initial_balance: float, now: datetime) -> int:
    """CLI 用の人間発行 (scope='human_custom' / issued_by='human_cli' 固定)。

    scope 引数を持たない — in_sample を呼び出し引数から偽装できない
    (TypeError で弾かれる)。
    """
    return _insert(
        conn, scope="human_custom", issued_by="human_cli",
        plugin_ref=plugin_ref, content_hash=content_hash, kind=kind,
        pair=pair, timeframe=timeframe, source=source, period=period,
        metrics=metrics, settings_hash=settings_hash, core_commit=core_commit,
        initial_balance=initial_balance, now=now)


def in_sample_view(conn: sqlite3.Connection, *,
                    pair: str | None = None) -> list[dict]:
    """改善ループが読む唯一の成績面。period_start/period_end は含めない。"""
    query = (f"SELECT {_VIEW_COLUMNS} FROM backtest_runs "
             "WHERE scope='in_sample' AND issued_by='harness'")
    params: list[Any] = []
    if pair is not None:
        query += " AND pair=?"
        params.append(pair)
    query += " ORDER BY created_at, id"
    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        metrics_json = d.pop("metrics_json")
        raw_metrics = json.loads(metrics_json)
        # fix round 1 F1 (codex Important): metrics dict は save_harness_run
        # の任意入力なので、"period_start"/"period_end" 等を metrics に
        # 混入されると列遮断 (holdout 遮断 1) を密輸できてしまう。読み側で
        # METRIC_KEYS の白リスト濾過をかける (遮断境界は view 側)。
        d["metrics"] = {k: v for k, v in raw_metrics.items()
                        if k in METRIC_KEYS}
        result.append(d)
    return result


def settings_snapshot_hash(settings: Any) -> str:
    """risk / backtest 設定の安定 JSON の sha256 hexdigest (再現性メタデータ)。

    Settings に sizing 独立セクションは無い — sizing 関連は risk 内
    (risk_per_trade_pct 等)、spread フォールバックは
    risk.pair_rules[].assumed_spread_pips に含まれるため risk のみで足りる。
    """
    payload = {
        "risk": settings.risk.model_dump(),
        "backtest": settings.backtest.model_dump(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def core_commit() -> str:
    """現在の HEAD commit hash。取得不能時は "unknown" (fail-soft — 再現性
    メタデータの欠損は保存自体を止める理由にならない)。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()

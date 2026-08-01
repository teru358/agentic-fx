"""analysis_runs — 履歴分析 (backtest/analysis.py) の実行記録 (プラン 6 Task 10)。

``analyze_for_agent`` (改善ループへ露出する唯一の分析面) が成功時に毎回
1 行保存する監査サイドチャネル。改善ループへの返り値には期間・件数・
境界日時を含めない (§6 遮断) が、このテーブルは人間側の監査用途なので
``params`` に期間 (``in_sample_until``) をそのまま記録してよい
(``store/backtest_runs.py`` の ``_insert`` の aware 検証・isoformat 慣例に
倣う)。

``trial_count`` は「その呼び出しで計算した相関値の個数」— 多重比較の分母
として使う (corr_matrix はペア数、lead_lag は ``len(LAGS)``、
rolling_corr_summary は窓の個数)。0 以下・非 int (bool 含む) は拒否する
(計算していない呼び出しを分母に混ぜない — エラー時は呼び出し元がそもそも
save を呼ばない契約)。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _require_utc(dt: datetime, what: str) -> datetime:
    """tz-aware datetime は UTC に正規化 (naive は fail closed)。

    プロジェクト方針 (store/backtest_runs.py の ``_require_utc`` と同じ規律):
    「naive を UTC とみなす」処理は禁止。
    """
    if dt.tzinfo is None:
        raise ValueError(f"{what} is naive; tz-aware UTC datetime required "
                         "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


def save(conn: sqlite3.Connection, *, params: dict[str, Any], trial_count: int,
         source: str, now: datetime) -> int:
    """analysis_runs へ 1 行保存し、lastrowid (analysis_run_id) を返す。"""
    if not isinstance(trial_count, int) or isinstance(trial_count, bool) \
            or trial_count < 1:
        raise ValueError(f"trial_count must be an int >= 1: {trial_count!r}")
    now_utc = _require_utc(now, "now")
    params_json = json.dumps(params, sort_keys=True)
    cur = conn.execute(
        "INSERT INTO analysis_runs (params_json, trial_count, source, "
        "created_at) VALUES (?,?,?,?)",
        (params_json, trial_count, source, now_utc.isoformat()))
    conn.commit()
    return cur.lastrowid

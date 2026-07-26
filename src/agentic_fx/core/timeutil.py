"""共有日時ユーティリティ。naive datetime を拒否し UTC に正規化する。"""
from __future__ import annotations

from datetime import datetime, timezone


def as_utc(dt: datetime) -> datetime:
    """入力を UTC に正規化。naive datetime は ValueError を送出。"""
    if dt.tzinfo is None:
        raise ValueError("timezone-aware な datetime が必要です")
    return dt.astimezone(timezone.utc)

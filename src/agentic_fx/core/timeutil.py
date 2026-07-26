"""共有日時ユーティリティ。naive datetime を拒否し UTC に正規化する。"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def as_utc(dt: datetime) -> datetime:
    """入力を UTC に正規化。naive datetime は ValueError を送出。"""
    if dt.tzinfo is None:
        raise ValueError("timezone-aware な datetime が必要です")
    return dt.astimezone(timezone.utc)


def to_display(dt: datetime, tz_name: str) -> datetime:
    """保存された UTC 時刻を表示用タイムゾーンへ変換する (表示層専用)。

    内部保存・判断ロジックは常に UTC のまま変更しない。この関数はログ・
    status 表示などユーザーが読む場面でのみ使う (呼び出し元はプラン 5 で追加)。
    naive datetime は ValueError (as_utc と同じ規約)。不正な tz_name の例外は
    素通しする (config ロード時に IANA タイムゾーンとして検証済みのため)。
    """
    if dt.tzinfo is None:
        raise ValueError("timezone-aware な datetime が必要です")
    return dt.astimezone(ZoneInfo(tz_name))

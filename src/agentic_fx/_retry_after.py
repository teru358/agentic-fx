"""HTTP 429/503 の Retry-After ヘッダを待ち時間にする共通ヘルパー。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx


def retry_after_from(exc: BaseException, now: datetime) -> timedelta | None:
    """429/503 の Retry-After (秒数または HTTP 日付) を待ち時間にする。

    読めない値・過去の日付・該当しない例外は None (定期間隔だけで待つ)。
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code not in (429, 503):
        return None
    raw = exc.response.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - now).total_seconds()
    if seconds != seconds or seconds <= 0:     # NaN・0 以下
        return None
    return timedelta(seconds=min(seconds, 86400.0))   # inf 等で overflow させない

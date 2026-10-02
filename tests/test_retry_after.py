from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agentic_fx._retry_after import retry_after_from

NOW = datetime(2026, 10, 2, 10, 38, tzinfo=timezone.utc)


def _err(status, value):
    req = httpx.Request("GET", "https://example.invalid/")
    headers = {} if value is None else {"Retry-After": value}
    return httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(status, headers=headers, request=req))


@pytest.mark.parametrize("value,expected", [
    ("120", timedelta(seconds=120)),
    ("Fri, 02 Oct 2026 11:38:00 GMT", timedelta(hours=1)),
    ("Fri, 02 Oct 2026 09:00:00 GMT", None),     # 過去
    ("0", None), ("-5", None), ("soon", None), ("nan", None),
    ("inf", timedelta(days=1)),
    (None, None),
])
def test_retry_after_value_parsing(value, expected):
    assert retry_after_from(_err(429, value), NOW) == expected


def test_only_429_and_503_count():
    assert retry_after_from(_err(500, "60"), NOW) is None
    assert retry_after_from(_err(503, "60"), NOW) == timedelta(seconds=60)
    assert retry_after_from(OSError("x"), NOW) is None

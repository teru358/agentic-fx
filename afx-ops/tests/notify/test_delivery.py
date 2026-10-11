from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from afx_ops.notify.delivery import (DeliveryError, classify_status, encode_json_object,
                                     request_headers, validate_webhook_url)


def test_https_url_is_normalized_to_the_only_permitted_port():
    target = validate_webhook_url(b"https://hooks.example.test/a?key=x")
    assert (target.url, target.host, target.port) == (
        "https://hooks.example.test/a?key=x", "hooks.example.test", 443)


def test_json_request_always_has_explicit_content_headers():
    body = encode_json_object({"event_id": 1})
    assert body == b'{"event_id":1}'
    assert dict(request_headers(body)) == {
        "Content-Type": "application/json", "Content-Length": "14"}


@pytest.mark.parametrize("url", [
    b"http://hooks.example.test/", b"https://hooks.example.test:444/",
    b"https://user@hooks.example.test/", b"https://hooks.example.test/#fragment",
    b"https:///missing-host", b"\xff",
])
def test_invalid_webhook_urls_are_rejected_without_exposing_them(url):
    with pytest.raises(DeliveryError) as raised:
        validate_webhook_url(url)
    assert raised.value.reason == "url_invalid"


@pytest.mark.parametrize(("status", "reason", "retryable"), [
    (204, None, False), (302, "redirect", False), (400, "http_4xx", False),
    (429, "http_429", True), (503, "http_5xx", True),
])
def test_status_classification_keeps_redirects_and_permanent_4xx_out_of_retry(status, reason, retryable):
    if reason is None:
        assert classify_status(status) is None
    else:
        result = classify_status(status)
        assert (result.reason, result.retryable) == (reason, retryable)


@pytest.mark.parametrize(("header", "seconds"), [
    ("30", 30.0), (" 7 ", 7.0), ("0", 0.0), ("-5", 0.0), ("1.5", 1.5),
    ("nonsense", None), ("", None), (None, None), ("nan", None), ("inf", None),
])
def test_429_carries_the_retry_after_seconds_when_it_is_readable(header, seconds):
    result = classify_status(429, header)
    assert (result.reason, result.retryable, result.retry_after) == ("http_429", True, seconds)


def test_retry_after_may_be_an_http_date():
    now = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert classify_status(429, header, now=now).retry_after == 45.0
    past = format_datetime(now - timedelta(seconds=45), usegmt=True)
    assert classify_status(429, past, now=now).retry_after == 0.0


def test_retry_after_is_only_read_for_429():
    assert classify_status(503, "30").retry_after is None

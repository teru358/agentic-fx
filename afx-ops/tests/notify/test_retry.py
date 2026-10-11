from __future__ import annotations

import pytest

from afx_ops.notify.retry import (MAX_ATTEMPTS, RetryError, WarningThrottle, retry,
                                  retry_delays)


def test_transient_failures_use_bounded_exponential_delays_then_succeed():
    calls = []
    sleeps = []

    def operation():
        calls.append(1)
        if len(calls) < 4:
            raise RetryError("timeout", retryable=True)
        return "sent"

    assert retry(operation, sleep=sleeps.append) == "sent"
    assert len(calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_permanent_failure_is_not_retried():
    with pytest.raises(RetryError) as raised:
        retry(lambda: (_ for _ in ()).throw(RetryError("http_4xx", retryable=False)),
              sleep=lambda _delay: pytest.fail("permanent failure slept"))
    assert raised.value.reason == "http_4xx"


def test_retry_exhaustion_makes_exactly_eight_attempts():
    calls = []
    with pytest.raises(RetryError) as raised:
        retry(lambda: (calls.append(1), (_ for _ in ()).throw(RetryError("tls", retryable=True)))[1],
              sleep=lambda _delay: None)
    assert raised.value.reason == "retry_exhausted"
    assert len(calls) == MAX_ATTEMPTS == 8
    assert retry_delays() == (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)


def test_same_failure_warning_is_throttled_but_a_different_reason_is_reported():
    throttle = WarningThrottle()

    assert throttle.allow("timeout", 0.0) is True
    assert throttle.allow("timeout", 30.0) is False
    assert throttle.allow("tls", 30.0) is True
    assert throttle.allow("timeout", 60.0) is True


def test_retry_after_longer_than_the_backoff_is_waited_and_capped_at_the_maximum():
    sleeps = []
    calls = []

    def operation():
        calls.append(1)
        if len(calls) <= 3:
            raise RetryError("http_429", retryable=True,
                             retry_after=(30.0, 5.0, 3600.0)[len(calls) - 1])
        return "sent"

    assert retry(operation, sleep=sleeps.append) == "sent"
    # 1 回目: backoff 1 < 30、2 回目: backoff 2 < 5、3 回目: 3600 は上限 60 で切る
    assert sleeps == [30.0, 5.0, 60.0]


def test_a_shorter_retry_after_does_not_shorten_the_backoff():
    sleeps = []
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise RetryError("http_429", retryable=True, retry_after=0.0)
        return "sent"

    assert retry(operation, sleep=sleeps.append) == "sent"
    assert sleeps == [1.0, 2.0]

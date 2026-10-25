from datetime import datetime, timedelta, timezone

from agentic_fx.store.news_health import Health, effective_wait, next_record, stage


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def test_stage_has_bounded_exponential_thresholds():
    assert stage(2) is None
    assert stage(3) == timedelta(hours=1)
    assert stage(4) == timedelta(hours=2)
    assert stage(7) == timedelta(hours=16)
    assert stage(8) == timedelta(hours=24)
    assert stage(10**9) == timedelta(hours=24)


def test_refusal_preserves_failures_and_success_resets_both():
    failed = next_record(Health(consecutive_failures=2), "failure", NOW, reason="HTTP 500")
    refused = next_record(failed, "refusal", NOW, reason="HTTP 429")
    assert (refused.consecutive_failures, refused.retry_after_streak) == (3, 1)
    assert refused.backoff_until == NOW + timedelta(hours=1)
    assert next_record(refused, "ok", NOW, count=0).consecutive_failures == 0


def test_effective_wait_caps_future_components_at_one_day():
    h = Health(backoff_until=NOW + timedelta(days=10), backoff_cause="failures")
    assert effective_wait(h, None, NOW) == (NOW + timedelta(hours=24), "failures")

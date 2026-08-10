from agentic_fx.core.health_latch import HealthLatch


def test_record_failure_latches_for_process_lifetime():
    latch = HealthLatch()
    assert latch.is_latched() is False
    assert latch.summary() == []

    latch.record_failure("disk full")
    latch.record_failure("second failure")

    assert latch.is_latched() is True
    assert latch.summary() == ["disk full", "second failure"]

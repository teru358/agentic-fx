"""認証失敗などの拒否記録: 分類ごとに 1 分 5 件、30 秒周期・分境界・停止で集約を書く。"""
from __future__ import annotations

from datetime import datetime, timezone


def _lines(env):
    if not env.activity_path.exists():
        return []
    return env.activity_path.read_text(encoding="utf-8").splitlines()


def test_rejections_keep_five_per_minute_and_flush_on_the_minute_boundary(ops_env):
    ops_env.wall.now = datetime(2026, 10, 5, 0, 0, 59, tzinfo=timezone.utc)
    service = ops_env.service()
    for _ in range(100):
        service.record_rejection("authentication_failed")
    assert len([l for l in _lines(ops_env) if "\tauthentication_failed\t" in l]) == 5
    # 30 秒周期の呼出し。分境界を越える前は何も書かない。
    service.flush_rejections()
    assert not [l for l in _lines(ops_env) if "\tsuppressed\t" in l]
    ops_env.wall.now = datetime(2026, 10, 5, 0, 1, 0, tzinfo=timezone.utc)
    service.flush_rejections()
    suppressed = [l for l in _lines(ops_env) if "\tsuppressed\t" in l]
    assert len(suppressed) == 1 and "authentication_failed: 95 suppressed" in suppressed[0]


def test_rejections_are_flushed_at_shutdown(ops_env):
    service = ops_env.service()
    for _ in range(8):
        service.record_rejection("peer_rejected")
    service.shutdown(join_timeout=1)
    suppressed = [l for l in _lines(ops_env) if "\tsuppressed\t" in l]
    assert len(suppressed) == 1 and "peer_rejected: 3 suppressed" in suppressed[0]


def test_rejections_in_a_new_minute_get_their_own_five_before_the_periodic_flush(ops_env):
    ops_env.wall.now = datetime(2026, 10, 5, 0, 0, 59, tzinfo=timezone.utc)
    service = ops_env.service()
    for _ in range(7):
        service.record_rejection("authentication_failed")
    ops_env.wall.now = datetime(2026, 10, 5, 0, 1, 0, tzinfo=timezone.utc)
    service.record_rejection("authentication_failed")
    lines = _lines(ops_env)
    assert len([l for l in lines if "\tauthentication_failed\t" in l]) == 6
    suppressed = [l for l in lines if "\tsuppressed\t" in l]
    assert len(suppressed) == 1 and "authentication_failed: 2 suppressed" in suppressed[0]


def test_only_categories_over_five_are_reported_as_suppressed(ops_env):
    service = ops_env.service()
    for _ in range(7):
        service.record_rejection("authentication_failed")
    for _ in range(3):
        service.record_rejection("peer_rejected")
    service.shutdown(join_timeout=1)
    suppressed = [l for l in _lines(ops_env) if "\tsuppressed\t" in l]
    assert len(suppressed) == 1 and "authentication_failed: 2 suppressed" in suppressed[0]


import pytest  # noqa: E402


@pytest.mark.parametrize("category", [
    "認証失敗の自由文", "authentication failed", "authentication_failed\tx", "unknown_kind",
    "", "x" * 65])
def test_rejection_category_outside_the_fixed_set_is_refused_without_writing(ops_env,
                                                                             category):
    service = ops_env.service()
    service.record_rejection("authentication_failed")
    before = _lines(ops_env)
    with pytest.raises(ValueError):
        service.record_rejection(category)
    service.shutdown(join_timeout=1)
    assert _lines(ops_env) == before

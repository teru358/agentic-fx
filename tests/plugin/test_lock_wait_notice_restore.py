"""The lock-wait callback is scoped: whatever was installed comes back afterwards."""
from __future__ import annotations

import pytest

from agentic_fx.plugin import switch


@pytest.fixture(autouse=True)
def _clean():
    previous = switch.set_plugin_lock_wait_notice(None)
    yield
    switch.set_plugin_lock_wait_notice(previous)


def test_setting_a_notice_returns_the_one_that_was_installed_before():
    def first():
        pass

    assert switch.set_plugin_lock_wait_notice(first) is None
    assert switch.set_plugin_lock_wait_notice(None) is first


def test_nested_scopes_restore_the_outer_notice_each_time():
    def outer():
        pass

    def inner():
        pass

    with switch.lock_wait_notice(outer):
        with switch.lock_wait_notice(inner):
            assert switch._plugin_lock_wait_notice is inner
        assert switch._plugin_lock_wait_notice is outer
    assert switch._plugin_lock_wait_notice is None


def test_an_exception_inside_the_scope_still_restores_the_previous_notice():
    def outer():
        pass

    switch.set_plugin_lock_wait_notice(outer)
    with pytest.raises(RuntimeError):
        with switch.lock_wait_notice(lambda: None):
            raise RuntimeError("boom")
    assert switch._plugin_lock_wait_notice is outer

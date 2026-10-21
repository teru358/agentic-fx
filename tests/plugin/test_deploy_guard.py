"""配備ガード: 配備操作は共有、質検査の母集団確定は排他。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.plugin import switch


def test_deploy_operations_do_not_exclude_each_other(tmp_path):
    root = tmp_path / "plugins"
    inside = threading.Event()
    release = threading.Event()

    def other():
        with switch._plugin_lock(root, "beta"):
            inside.set()
            release.wait(10)

    thread = threading.Thread(target=other)
    thread.start()
    try:
        assert inside.wait(5)
        with switch._plugin_lock(root, "alpha", deadline=_soon()):
            pass
    finally:
        release.set()
        thread.join(10)


def _soon():
    import time
    return time.monotonic() + 2


def test_a_plugin_lock_excludes_the_exclusive_guard(tmp_path):
    root = tmp_path / "plugins"
    with switch._plugin_lock(root, "alpha"):
        import time
        with pytest.raises(switch.PluginBusyError):
            with switch.deploy_guard(
                    root, exclusive=True, deadline=time.monotonic() + 0.2):
                pass


def test_the_exclusive_guard_excludes_a_plugin_lock_until_released(tmp_path):
    import time
    root = tmp_path / "plugins"
    with switch.deploy_guard(root, exclusive=True):
        with pytest.raises(switch.PluginBusyError):
            with switch._plugin_lock(
                    root, "alpha", deadline=time.monotonic() + 0.2):
                pass
    with switch._plugin_lock(root, "alpha", deadline=time.monotonic() + 2):
        pass


def test_the_guard_file_is_not_collected_as_an_orphan_lock(tmp_path):
    assert not switch.deploy_guard_path(tmp_path).name.endswith(".lock")

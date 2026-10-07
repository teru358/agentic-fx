from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.plugin.switch import PluginBusyError, _plugin_lock


def test_ac33_bounded_plugin_lock_honors_deadline(tmp_path):
    root = tmp_path / "plugins"
    entered, release = threading.Event(), threading.Event()

    def holder():
        with _plugin_lock(root, "alpha"):
            entered.set()
            release.wait()

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(PluginBusyError):
            with _plugin_lock(root, "alpha", deadline=time.monotonic() + 0.05):
                pass
    finally:
        release.set()
        thread.join(1)

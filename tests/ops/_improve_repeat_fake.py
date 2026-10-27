"""improve の繰り返しを試す ops 層 test 用の supervisor 差し替え。"""
from __future__ import annotations

import threading

from agentic_fx.core.improve_supervisor import RepeatResult


class FakeSupervisor:
    """submit_manual / submit_repeat の呼び出しを記録し、`repeat` で挙動を差し替える。"""

    def __init__(self, repeat=None):
        self.manual_calls = []
        self.repeat_calls = []
        self.repeat = repeat
        self.entered = threading.Event()

    def submit_manual(self, **kwargs):
        self.manual_calls.append(dict(kwargs))
        if kwargs.get("on_prepared") is not None:
            kwargs["on_prepared"](17)
        return 17

    def submit_repeat(self, **kwargs):
        self.repeat_calls.append(dict(kwargs))
        self.entered.set()
        if self.repeat is not None:
            return self.repeat(**kwargs)
        return RepeatResult("max_runs", 1, 17)

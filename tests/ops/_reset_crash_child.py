"""kill switch reset の途中で process を落とす子。

usage: _reset_crash_child.py <app_state.json> <expected_generation> <point>
point: after_marker (state replace の前) / after_replace (marker 削除の前) /
after_unlink (marker 削除の直後)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from agentic_fx.store.state import StateStore


def main() -> int:
    path, generation, point = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    store = StateStore(path)
    if point == "after_marker":
        store.save = lambda *a, **k: os._exit(17)
    elif point == "after_replace":
        store._remove_marker = lambda *a, **k: os._exit(17)
    elif point == "after_unlink":
        original = store._remove_marker

        def remove_then_die(*a, **k):
            original(*a, **k)
            os._exit(17)

        store._remove_marker = remove_then_die
    store.reset_kill_switch(generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

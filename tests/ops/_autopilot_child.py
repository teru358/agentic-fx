"""別 process (対話シェルや他の CLI に相当) として autopilot を true にする子。

usage: _autopilot_child.py <app_state.json> now
       _autopilot_child.py <app_state.json> hold <release_file>
- now: StateStore.update(autopilot=True) をそのまま実行する
- hold: update の flock を取って書く直前で止まり ("holding" を出す)、<release_file> が
  現れてから書いて flock を放す
終わったら "done" を出す。
"""
from __future__ import annotations

import resource
import sys
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from agentic_fx.store.state import StateStore  # noqa: E402


def main() -> int:
    store = StateStore(Path(sys.argv[1]))
    if sys.argv[2] == "hold":
        release = Path(sys.argv[3])
        original = store.save

        def save_after_release(state):
            print("holding", flush=True)
            end = time.monotonic() + 60
            while not release.exists():
                if time.monotonic() > end:
                    raise SystemExit("release file never appeared")
                time.sleep(0.01)
            original(state)

        store.save = save_after_release
    store.update(autopilot=True)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

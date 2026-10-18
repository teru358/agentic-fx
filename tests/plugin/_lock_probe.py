"""Child entry: try each plugin name lock with LOCK_EX|LOCK_NB and print who was free.

argv: plugins_root name [name ...]
stdout: one JSON object {name: acquired}.
"""
import fcntl
import json
import sys
from pathlib import Path

from agentic_fx.plugin.switch import plugin_lock_path


def main() -> None:
    root = Path(sys.argv[1])
    result = {}
    for name in sys.argv[2:]:
        path = plugin_lock_path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result[name] = False
        else:
            result[name] = True
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()

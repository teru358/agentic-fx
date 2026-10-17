"""Child entry: try each plugin name lock with LOCK_EX|LOCK_NB and print who was free.

argv: plugins_root name [name ...]
stdout: one JSON object {name: acquired}.
"""
import fcntl
import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1])
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for name in sys.argv[2:]:
        handle = open(lock_dir / f"{name}.lock", "a+")
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

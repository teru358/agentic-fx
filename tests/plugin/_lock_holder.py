"""Child entry: hold plugin name locks until the parent releases it.

argv: plugins_root barrier_prefix name [name ...]
Writes <prefix>.held once every lock is owned, exits after <prefix>.release exists.
"""
import fcntl
import sys
import time
from pathlib import Path


def main() -> None:
    root, prefix = Path(sys.argv[1]), sys.argv[2]
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles = []
    for name in sorted(sys.argv[3:]):
        handle = open(lock_dir / f"{name}.lock", "a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        handles.append(handle)
    Path(prefix + ".held").write_text("held")
    deadline = time.monotonic() + 60
    while not Path(prefix + ".release").exists():
        if time.monotonic() > deadline:
            raise SystemExit("release was never signalled")
        time.sleep(0.01)


if __name__ == "__main__":
    main()

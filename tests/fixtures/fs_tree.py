"""Directory tree snapshots for zero-write contracts."""
from __future__ import annotations

import os
from pathlib import Path


def tree_snapshot(root: Path, *, ignore: frozenset[str] = frozenset()) -> dict[str, tuple]:
    """Every path under ``root`` with its type and byte content (symlinks by target)."""
    snapshot: dict[str, tuple] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirs + files:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative in ignore:
                continue
            if path.is_symlink():
                snapshot[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                snapshot[relative] = ("dir",)
            else:
                snapshot[relative] = ("file", path.read_bytes())
    return snapshot

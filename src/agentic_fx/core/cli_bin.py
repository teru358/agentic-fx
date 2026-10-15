"""Resolve a configured CLI binary without ever consulting the working directory.

Shared by the setup checks, the runner factory and the mission worker so that
the three agree on what a configured ``bin`` value may point at.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path


class CliBinNotFound(RuntimeError):
    """A configured CLI binary could not be resolved to an absolute path."""

    def __init__(self, configured: str) -> None:
        self.configured = configured
        super().__init__("runner.claude.bin が見つかりません (bin_not_found)")


def resolve_cli_bin(value: str, *, which: Callable[[str], str | None] | None = None) -> Path | None:
    """Return an absolute configured CLI path, rejecting relative resolution.

    ``~`` is expanded first.  An absolute path is returned as is.  A relative
    path containing ``/`` is never resolved against the working directory.  A
    bare name goes through ``which`` and the result must itself be absolute
    (a relative ``PATH`` element would otherwise point into the cwd)."""
    lookup = shutil.which if which is None else which
    configured = Path(value).expanduser()
    if configured.is_absolute():
        return configured
    if "/" in value:
        return None
    found = lookup(value)
    if found is None:
        return None
    candidate = Path(found)
    return candidate if candidate.is_absolute() else None

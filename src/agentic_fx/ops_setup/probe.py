"""Safe parent-side boundary for the fixed ``afx-ops probe`` protocol."""
from __future__ import annotations

import os
import re
import shlex
import subprocess


_FIXED_REASONS = frozenset({
    "config_dir_missing", "config_dir_invalid", "config_invalid", "secrets_failed",
    "url_invalid", "token_invalid", "startup_internal_error", "probe_not_implemented",
    "runtime_unavailable", "probe_bind_failed",
})
_PROBE_REASON = re.compile(r"probe_[a-z][a-z0-9_]{0,63}\Z")


def _reason(stderr: str) -> str:
    """Accept exactly the first line of the child protocol, never child diagnostics."""
    line = stderr.splitlines()[0] if stderr.splitlines() else ""
    word = line.removeprefix("afx-ops: ")
    if line.startswith("afx-ops: ") and (word in _FIXED_REASONS or _PROBE_REASON.fullmatch(word)):
        return word
    return "feature_probe_unreadable"


def run_probe(ops_exec: str, feature: str, *, timeout: float = 30.0) -> tuple[int, str]:
    """Run one bounded probe without passing parent secrets or diagnostics through."""
    try:
        completed = subprocess.run(
            [*shlex.split(ops_exec), "probe", feature], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=False, check=False,
            timeout=timeout, env={"PATH": os.defpath},
        )
    except subprocess.TimeoutExpired:
        return 73, "feature_probe_timeout"
    except OSError:
        return 70, "runtime_unavailable"
    return completed.returncode, _reason(completed.stderr.decode("utf-8", errors="replace"))

"""Temporary local entry point for ops reconfiguration."""
from __future__ import annotations

import sys
from pathlib import Path


def run_ops_reconfigure(root: Path, feature: str) -> int:
    del root, feature
    print("afx ops: reconfigure_not_available", file=sys.stderr)
    return 1

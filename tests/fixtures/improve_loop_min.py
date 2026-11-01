"""最小の `ImproveLoop` (conn は呼び出し元が渡す sqlite、root は tmp_path)。
tests/loops と tests/integration が同じ構築を使う。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
_EXAMPLE_SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
# 履歴を要する試験は評価用の履歴源を明示する (example の既定は未設定)。
SETTINGS = _EXAMPLE_SETTINGS.model_copy(update={
    "backtest": _EXAMPLE_SETTINGS.backtest.model_copy(
        update={"eval_source": "dukascopy"})})


class FakeRag:
    """`ImproveLoop.__init__(rag=...)` を満たすだけの no-op。"""


def build_loop(conn, root: Path, *, clock=None, settings=None):
    from agentic_fx.loops.improve_loop import ImproveLoop

    return ImproveLoop(
        root=root, settings=settings or SETTINGS, clock=clock or FixedClock(NOW),
        db_write_conn_factory=lambda: conn,
        db_readonly_conn_factory=lambda: conn,
        activity=ActivityLog(root / "activity.log"), rag=FakeRag())

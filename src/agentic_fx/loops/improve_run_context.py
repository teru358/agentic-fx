"""ImproveRunContext — improve Mission 1 回分の不変コンテキスト
(設計書 §4 prepare、プラン §8.1-6)。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger


@dataclass(frozen=True)
class ImproveRunContext:
    mission_id: int
    run_id: int
    staging_dir: Path
    source_snapshot_dir: Path
    allowed_backlog_ids: frozenset[int] | None
    slot_key: tuple[str, int] | None   # wave 起動なら (period_key, k)。手動は None
                                        # (レビュー1周目 C2)
    ledger: ImproveRpcLedger
    rpc_handlers: dict[str, Callable[[dict], dict]]

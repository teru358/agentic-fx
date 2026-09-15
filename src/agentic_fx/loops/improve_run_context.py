"""ImproveRunContext — improve Mission 1 回分の不変コンテキスト
(設計書 §4 prepare、プラン §8.1-6)。"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    # [indicator-consumption-wiring] codex plan r2 束3 Critical: T5a Step
    # 5-1 の C4 フィールド追加を T4b へ前倒し (既存 8 フィールドは無変更)。
    # `prepare()` が非空の値を書き込む実配線は T5a の仕事のまま — T4b の
    # 時点では常に既定値のまま届く。
    inventory: "InventoryBuildResult | None" = None
    inventory_view: dict = field(default_factory=dict)

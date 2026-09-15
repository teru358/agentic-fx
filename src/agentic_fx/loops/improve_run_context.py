"""ImproveRunContext — improve Mission 1 回分の不変コンテキスト
(設計書 §4 prepare、プラン §8.1-6)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult


def _empty_inventory_result() -> InventoryBuildResult:
    """[indicator-consumption-wiring] T5b 逸脱是正: `commit()` (Step 5-5)
    が `ctx.inventory.inventory` を無条件で読むようになったため、
    `ImproveRunContext` を直組みする既存テスト (T5a/T4b 以前からの多数の
    `ImproveRunContext(...)` 呼び出し、`inventory=` を省略) が
    `AttributeError` で退行した (実測で確認)。`prepare()` 経由の本番経路は
    T5a Step 5-1 以降常に非空の `InventoryBuildResult` を書き込むので、
    直組みの既定値も「未設定 (`None`)」ではなく「空の inventory」にする方が
    実態に合う — `meta.indicators` が空 (依存なし strategy) のテストは
    この空 inventory のままで `resolve_indicator_deps` が素通りする。"""
    return InventoryBuildResult(
        inventory=ApprovedInventory(root=Path(".").resolve(), metas=()),
        phase1_metas=(), resolved={}, rejected_strategies=())


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
    # `prepare()` が非空の値を書き込む実配線は T5a の仕事 (完了済み)。
    inventory: InventoryBuildResult = field(default_factory=_empty_inventory_result)
    inventory_view: dict = field(default_factory=dict)

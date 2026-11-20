"""指標の棚卸し (usage_review) は人が読む面だけのもので、改善 agent の入力に流れない。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_fx.loops.improve_loop import build_inventory_view
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult

SRC = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
AGENT_FACING = ("loops", "tools", "runners")
FORBIDDEN = ("usage_review", "read_model", "usage_inventory")


def test_the_improve_inventory_view_has_no_usage_review():
    result = InventoryBuildResult(
        inventory=ApprovedInventory(root=Path("/nonexistent"), metas=()),
        phase1_metas=(), resolved={}, rejected_strategies=())
    view = build_inventory_view(result)
    assert "usage_review" not in view
    assert "usage_review" not in json.dumps(view)
    assert set(view) == {"plugins", "pin_broken_strategies"}


@pytest.mark.parametrize("directory", AGENT_FACING)
def test_no_module_that_builds_agent_input_reads_the_usage_review(directory):
    offenders = []
    for path in sorted((SRC / directory).rglob("*")):
        if path.suffix not in (".py", ".md") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.relative_to(SRC)}:{word}" for word in FORBIDDEN if word in text)
    assert offenders == []

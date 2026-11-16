"""bridge 案内の履歴取り込み手順は、主 pair と参照を同じ開始日・設定足幅で、daemon 停止中に導入する。"""
from __future__ import annotations

from agentic_fx.setup import wizard


def _step() -> str:
    return next(line for line in wizard._BRIDGE_GUIDE if line.lstrip().startswith("7."))


def test_the_import_step_covers_the_pair_and_references_with_the_configured_width():
    step = _step()
    assert "datafeed.watch_symbols" in step and "同じ開始日" in step
    assert "--interval <base_interval>" in step and "backtest.base_interval" in step
    assert "daemon を止めて" in step


def test_the_import_step_is_not_a_single_symbol_or_a_fixed_width_example():
    step = _step()
    assert "--interval 5m" not in step and "USDJPY" not in step
    assert "--symbol <銘柄>" in step


def test_later_updates_are_pointed_to_the_offline_sync():
    step = _step()
    assert "`uv run afx history sync`" in step and "holdout 内" in step
    assert "afx ctl history" not in step            # 自動同期 (段 3) はまだ導入されていない

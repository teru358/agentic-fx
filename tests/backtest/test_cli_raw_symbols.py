"""History and correlation commands accept a raw broker symbol unchanged."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentic_fx.entry import main

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_settings(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO_ROOT / "config" / "settings.yaml.example",
                root / "config" / "settings.yaml")


def test_history_import_coverage_and_corr_keep_their_contract_for_a_raw_symbol(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.import_mt5") as importer, \
         patch("agentic_fx.backtest.cli.coverage_report") as coverage, \
         patch("agentic_fx.backtest.cli.corr_matrix") as corr:
        importer.return_value = MagicMock(inserted=1, unchanged=0, conflicted=0)
        coverage.return_value = {"bars": 1, "expected_open_bars": 1, "gap_pct": 0.0}
        corr.return_value = {("JP225", "US500"): 0.5}
        assert main(["history", "import", "--source", "mt5", "--symbol", "JP225",
                     "--from", "2026-07-01", "--to", "2026-07-02"]) == 0
        assert main(["history", "coverage", "--symbol", "JP225", "--timeframe", "1h",
                     "--source", "mt5", "--from", "2026-07-01", "--to", "2026-07-02"]) == 0
        assert main(["analyze", "corr", "--a", "JP225", "--b", "US500",
                     "--timeframe", "1h", "--source", "mt5"]) == 0
    assert importer.call_args.args[1] == "JP225"
    assert importer.call_args.kwargs["interval"] == "1m"
    assert coverage.call_args.args[1] == "JP225"
    assert coverage.call_args.kwargs["dataset"].base_interval == "1m"
    assert corr.call_args.args[1] == ["JP225", "US500"]
    assert "0.5" in capsys.readouterr().out

"""materialize → _human 編集 → submit|bless --from _human の CLI E2E
(プラン 10 Task 11g、§8.1-37)。entry.main([...]) 経由 (既存 test_approval.py
の CLI 配線テストと同じ規約)。

B-14 是正: `run_gate_pytest` (B-6) は冒頭で `landlock.is_available()` を
確認し、非対応環境では `RuntimeError` で fail closed する。B-6 自身の
テスト規約 (`pytest.mark.skipif(not is_available(), ...)`) と同じ扱いを
このモジュールにも適用しつつ、CLI E2E は「配線」の確認が目的で実
Landlock 実行のコストを払う必要が無いため、既定は `run_gate_pytest` を
monkeypatch する (B-6/11d/11e の他テストと同じ規約)。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.entry import main
from agentic_fx.plugin.gate_pytest import GateResult


def _fake_pytest_ok(plugin_dir, *, settings):
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed", duration_sec=0.1)


def test_materialize_then_submit_from_human_cli_e2e(tmp_path, monkeypatch, capsys):
    root = tmp_path
    (root / "plugins").mkdir()
    plugin_dir = root / "plugins" / "sma"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (plugin_dir / "config.yaml").write_text("kind: indicator\n")
    (plugin_dir / "test_plugin.py").write_text("def test_x():\n    pass\n")
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_bytes(
        Path("config/settings.yaml.example").read_bytes())
    (root / "data").mkdir()

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.chdir(root)
    # 逸脱 (実測): プラン骨子は ensure_initialized の扱いに触れていない
    # (逐語) が、既存 CLI 配線テスト (test_approval.py) と同じ規約どおり
    # `agentic_fx.backtest.cli.ensure_initialized` を patch する必要が実測で
    # 判明した (state.json の初期化なしでは SystemExit(2) になる)。
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service"):
        rc = main(["plugin", "materialize", "sma"])
        assert rc == 0
        assert (root / "plugins" / "_human" / "sma").is_dir()

        # 編集 (test を書き換えて別 artifact_hash にする)
        (root / "plugins" / "_human" / "sma" / "test_plugin.py").write_text(
            "def test_x():\n    assert True\n")

        rc2 = main(["plugin", "submit", "--from", "_human", "sma"])
        assert rc2 == 0
        out = capsys.readouterr().out
        assert "承認申請" in out or "pending" in out.lower()


def test_bless_without_from_human_is_always_rejected(tmp_path, monkeypatch, capsys):
    root = tmp_path
    (root / "plugins").mkdir()
    plugin_dir = root / "plugins" / "sma"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (plugin_dir / "config.yaml").write_text("kind: indicator\n")
    (plugin_dir / "test_plugin.py").write_text("def test_x():\n    pass\n")
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_bytes(
        Path("config/settings.yaml.example").read_bytes())
    (root / "data").mkdir()
    monkeypatch.chdir(root)

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service"):
        rc = main(["plugin", "bless", "sma"])  # --from なし
        assert rc != 0
        # 検収 m9 是正: 他の全 CLI エラーと同じく stderr へ統一 (旧稿は stdout)
        err = capsys.readouterr().err
        assert "materialize" in err

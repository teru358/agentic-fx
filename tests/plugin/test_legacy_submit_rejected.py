"""`afx plugin submit` の旧経路 (live plugins/ を直接申請) は全 kind で共有 gate の前に
拒否し、`--from _human` だけが共有 gate を通る。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.entry import main
from agentic_fx.plugin.gate_pytest import GateResult

_CONFIGS = {
    "indicator": "kind: indicator\noutputs: [v]\n",
    "signal": "kind: signal\n",
    "strategy": "kind: strategy\n",
}


def _fake_pytest_ok(plugin_dir, *, settings):
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed",
                      duration_sec=0.1)


def _root(tmp_path: Path, kind: str) -> Path:
    plugin_dir = tmp_path / "plugins" / "legacy"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (plugin_dir / "config.yaml").write_text(_CONFIGS[kind])
    (plugin_dir / "test_plugin.py").write_text("def test_x():\n    pass\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_bytes(
        Path("config/settings.yaml.example").read_bytes())
    (tmp_path / "data").mkdir()
    return tmp_path


def _approval_rows(root: Path) -> int:
    db = root / "data" / "agentic.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM approval_requests").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _submit(root, monkeypatch, argv):
    monkeypatch.setattr("agentic_fx.plugin.approval.run_gate_pytest",
                        _fake_pytest_ok)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest",
                        _fake_pytest_ok)
    monkeypatch.chdir(root)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service"):
        return main(argv)


@pytest.mark.parametrize("kind", ["indicator", "signal", "strategy"])
def test_legacy_submit_is_rejected_for_every_kind_without_pending_rows(
        tmp_path, monkeypatch, capsys, kind):
    root = _root(tmp_path, kind)

    rc = _submit(root, monkeypatch, ["plugin", "submit", "legacy"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "--from _human" in err
    assert "materialize" in err
    assert _approval_rows(root) == 0


def test_legacy_submit_is_rejected_before_looking_up_the_plugin(
        tmp_path, monkeypatch, capsys):
    """存在しない名前でも、旧経路の検証 (plugin の探索・gate) には入らず同じ案内になる。"""
    root = _root(tmp_path, "indicator")

    rc = _submit(root, monkeypatch, ["plugin", "submit", "no-such-plugin"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "--from _human" in err
    assert "見つかりません" not in err


def test_legacy_submit_never_calls_the_legacy_gate(tmp_path, monkeypatch):
    root = _root(tmp_path, "indicator")

    def _boom(*a, **kw):
        raise AssertionError("legacy submit_plugin was called")

    monkeypatch.setattr("agentic_fx.plugin.approval.submit_plugin", _boom)

    rc = _submit(root, monkeypatch, ["plugin", "submit", "legacy"])

    assert rc == 1
    assert _approval_rows(root) == 0


def test_from_human_submit_still_goes_through_shared_gate(
        tmp_path, monkeypatch, capsys):
    root = _root(tmp_path, "indicator")
    assert _submit(root, monkeypatch, ["plugin", "materialize", "legacy"]) == 0
    capsys.readouterr()

    rc = _submit(root, monkeypatch,
                 ["plugin", "submit", "--from", "_human", "legacy"])

    assert rc == 0
    assert "承認申請" in capsys.readouterr().out
    assert _approval_rows(root) == 1


def test_no_entry_point_calls_the_legacy_submit_function():
    """旧 gate の関数を呼ぶ入口を src に増やさない (呼び出しの全数を grep で数える)。"""
    import re

    src = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
    pattern = re.compile(r"\bsubmit_plugin\s*\(")
    callers = []
    for path in src.rglob("*.py"):
        if path.name == "approval.py" and path.parent.name == "plugin":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if pattern.search(code) and not code.lstrip().startswith(
                    ('"', "'", "`")):
                callers.append(f"{path.name}: {line.strip()}")
    assert callers == []

"""``afx plugin rollback`` の履歴証跡の拒否と、人間専用の ``--force-history-guard``。"""
from __future__ import annotations

import pytest

from agentic_fx.backtest import plugin_deploy_cli
from agentic_fx.entry import main
from agentic_fx.plugin import loader
from agentic_fx.store import approvals as approvals_store
from tests.backtest.test_plugin_deploy_cli import CliWorld
from tests.history_support import ready_evidence
from tests.plugin.deploy_world import at


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = CliWorld(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: False)
    a, b = (built.version("sma", tag, kind="strategy") for tag in ("a", "b"))
    for version, previous, minute in ((a, None, 1), (b, a, 2)):
        payload = {"name": "sma", "kind": "strategy", "content_hash": loader.content_hash(version),
                   "artifact_hash": version.name}
        if version is b:
            payload["history_guard"] = ready_evidence()
        aid = approvals_store.create(built.conn, "plugin", payload, at(minute))
        approvals_store.apply_decision(built.conn, aid, "approved", decided_by="human",
                                       now=at(minute))
        built.journal("sma", "approve", previous, version, minute, aid)
    built.link("sma", b)
    built.old, built.new = a, b
    yield built
    built.conn.close()


def _run(capsys, *argv):
    rc = main(["plugin", "rollback", "sma", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _link(world):
    return str((world.root / "sma").readlink())


def test_a_plain_rollback_without_evidence_is_refused_with_the_forced_path_explained(
        world, capsys):
    rc, _out, err = _run(capsys, "--to", world.old.name, "--yes")
    assert rc == 1 and "code=history_not_ready" in err and "history_evidence_required" in err
    assert "--force-history-guard" in err and "--reason" in err
    assert _link(world) == world.target("sma", world.new)


def test_a_forced_rollback_switches_and_leaves_an_audit_record(world, capsys):
    rc, out, err = _run(capsys, "--to", world.old.name, "--force-history-guard",
                        "--reason", "休場を確認した", "--yes")
    assert rc == 0, err
    assert _link(world) == world.target("sma", world.old)
    log = (world.base / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "rollback_history_guard_forced" in log and "reason=休場を確認した" in log


@pytest.mark.parametrize("argv", [
    ["--force-history-guard", "--reason", "x"],                                  # 既定の対象
    ["--to", "{short}", "--force-history-guard", "--reason", "x"],               # 短縮 hash
    ["--to", "{full}", "--force-history-guard"],                                 # 理由なし
    ["--to", "{full}", "--force-history-guard", "--reason", "  "],               # 空の理由
    ["--to", "{full}", "--reason", "x"],                                         # フラグなしの理由
])
def test_an_incomplete_force_is_refused_before_anything_runs(world, capsys, argv):
    argv = [part.format(short=world.old.name[:12], full=world.old.name) for part in argv]
    rc, _out, err = _run(capsys, *argv, "--yes")
    assert rc == 1 and "code=invalid_request" in err
    assert _link(world) == world.target("sma", world.new)
    assert not (world.base / "logs" / "activity.log").exists() or (
        "history_guard_forced" not in (world.base / "logs" / "activity.log").read_text())


def test_the_force_flag_is_only_a_local_cli_option_not_an_ops_or_agent_surface():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    hits = [str(p.relative_to(root)) for base in (root / "src", root / "afx-ops" / "afx_ops")
            for p in base.rglob("*.py")
            if "force_history_guard" in p.read_text(encoding="utf-8")
            or "force-history-guard" in p.read_text(encoding="utf-8")]
    assert sorted(hits) == ["src/agentic_fx/backtest/cli.py",
                            "src/agentic_fx/backtest/plugin_deploy_cli.py",
                            "src/agentic_fx/plugin/deploy_rollback.py"]

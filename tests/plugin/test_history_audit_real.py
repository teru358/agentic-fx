"""非配備版の監査を、実 sandbox (Landlock・seccomp) と実 worker で入口から通す。

専用 work_root へのコピーが Landlock 下で読めること、gate の pytest と replay の worker が起動し
回収されること、コピーが後片付けされることを確かめる。合成の strategy と合成の履歴だけを使い、
すべて tmp_path の下に置く。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentic_fx.plugin import history_audit as audit
from agentic_fx.plugin import loader, sandbox
from tests.plugin.reevaluation_world import STARTED, RealWorld, strategy_py

pytestmark = pytest.mark.skipif(sandbox.host_preflight()[0] is not None,
                                reason="this host cannot isolate plugin workers")

NAME = "sma"


def _children() -> set[int]:
    out = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True)
    return {int(p) for p in out.stdout.split()}


def _run(world: RealWorld, tmp_path: Path, py: str):
    version = world.strategy("a", py, name=NAME)
    old_id = world.approve(NAME, version)
    target = audit.AuditTarget(old_id, NAME, version.name, loader.content_hash(version), {})
    work_root, report_dir = tmp_path / "work", tmp_path / "reports"
    result = audit.audit_non_deployed_version(
        world.conn, target, plugins_root=world.root, settings=world.settings,
        report_dir=report_dir, work_root=work_root, clock=lambda: STARTED)
    return result, work_root, report_dir


def test_the_real_sandbox_and_worker_evaluate_a_stored_copy_and_clean_up(tmp_path):
    world = RealWorld(tmp_path)
    before_children = _children()

    result, work_root, report_dir = _run(world, tmp_path, strategy_py())

    report = result.report
    assert result.write_error is None and result.report_path.parent == report_dir
    # コピーは Landlock 下の pytest と worker に読まれ、評価が最後まで進んだ
    assert report["axes"]["execution"]["status"] == "completed", report["result"]
    assert report["result"]["overall"] in ("pass", "fail"), report["result"]
    assert report["result"]["reason"] in (None, "floor")
    rows = {(r["scope"], r["pair"]): r for r in report["rows"]}
    assert rows[("in_sample", "USDJPY")]["trades"] >= 30
    assert ("holdout", "USDJPY") in rows
    assert report["axes"]["unexecuted"] == []
    # 一時領域は消え、worker は回収された
    assert [p.name for p in work_root.iterdir()] == []
    assert _children() <= before_children
    # 配備は作られず、成績は DB の結果表へ書かれない
    assert not (world.root / NAME).exists()
    assert world.conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_a_stored_copy_whose_own_test_fails_is_a_fail_through_the_real_sandbox(tmp_path):
    world = RealWorld(tmp_path)
    version = world.strategy("a", strategy_py(), name=NAME)
    (version / "test_plugin.py").write_text("def test_plugin():\n    assert False\n")
    # the artifact hash covers the test file, so store the edited bytes under their own hash
    digest = loader.artifact_hash_bytes(*(
        (version / f).read_bytes() for f in ("plugin.py", "config.yaml", "test_plugin.py")))
    final = version.with_name(digest)
    version.rename(final)
    old_id = world.approve(NAME, final)
    target = audit.AuditTarget(old_id, NAME, final.name, loader.content_hash(final), {})
    work_root = tmp_path / "work"

    result = audit.audit_non_deployed_version(
        world.conn, target, plugins_root=world.root, settings=world.settings,
        report_dir=tmp_path / "reports", work_root=work_root, clock=lambda: STARTED)

    assert result.report["result"]["overall"] == "fail"
    assert result.report["axes"]["floor"]["in_sample"]["result"] == "not_run"
    assert [p.name for p in work_root.iterdir()] == []

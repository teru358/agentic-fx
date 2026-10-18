"""実隔離の improve worker から配備操作に届かないことの統合テスト。

worker は production と同じ bootstrap 経路 (`_bootstrap_improve_profile`) で隔離した
子プロセス。その中から (a) `afx plugin disable / rollback / retire / revoke` の起動、
(b) DB への直接書込み、(c) `plugins/` の symlink 差し替えと `.versions` の改変を試み、
全てが拒否されて外側の tree (DB・plugins・設定・logs) が一切変わらないことを測る。
同じ操作を隔離なしで行うと tree が変わることを対照にして、拒否の理由が fixture の
不備でないことを示す。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import IMPROVE_MIN_ABI, landlock_abi
from tests.backtest.test_plugin_deploy_cli import CliWorld
from tests.integration.test_improve_worker_permission_boundary import (
    _mk_repo_layout, _run_probe)

pytestmark = pytest.mark.skipif(
    landlock_abi() < IMPROVE_MIN_ABI,
    reason="improve worker isolation needs Landlock ABI >= 8")

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
_AFX = Path(sys.executable).parent / "afx"


def _tree_state(root: Path) -> dict:
    """Every path under root with its kind, link target and content hash."""
    state: dict[str, object] = {}
    for current, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            path = Path(current) / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                state[rel] = ("symlink", os.readlink(path))
            elif path.is_dir():
                state[rel] = ("dir",)
            else:
                state[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return state


def _build_world(base: Path) -> tuple[CliWorld, dict]:
    world = CliWorld(base)
    a, b, c = world.three_generations("sma")
    (world.root / "legacy").mkdir()
    (world.root / "legacy" / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (world.root / "legacy" / "config.yaml").write_text("kind: indicator\noutputs: [value]\n")
    (world.root / "legacy" / "test_plugin.py").write_text("def test_plugin():\n    pass\n")
    world.conn.close()
    return world, {"a": world.content(a), "b": world.content(b), "b_dir": b.name, "c_dir": c.name}


_PRELUDE = textwrap.dedent("""
    import json, os, sqlite3, subprocess, sys
    RESULTS = []

    def record(label, outcome):
        RESULTS.append([label, outcome])
        print('RESULT ' + json.dumps([label, outcome]))
""")


def _cli_snippet(root: Path, argv: list[str]) -> str:
    return textwrap.dedent(f"""
        entrypoints = {{
            'console_script': [{str(_AFX)!r}],
            'module': [sys.executable, '-m', 'agentic_fx.entry'],
        }}
        env = {{'PATH': '/usr/bin:/bin', 'HOME': {str(root)!r},
               'PYTHONPATH': {str(_REPO_SRC)!r}}}
        for label, prefix in entrypoints.items():
            try:
                proc = subprocess.run(prefix + {argv!r}, cwd={str(root)!r}, env=env,
                                      capture_output=True, text=True, timeout=60)
                record(label, ['rc', proc.returncode, 'PermissionError' in proc.stderr])
            except OSError as exc:
                record(label, ['oserror', exc.errno])
    """)


def _db_snippet(root: Path) -> str:
    insert = ("INSERT INTO plugin_deploy_ops (op_kind, name, from_kind, to_kind, from_target, "
              "subject_content_hash, subject_artifact_hash, revoke, force, phase, "
              "notify_attempted, actor, created_at, updated_at) VALUES ('disable_symlink', "
              "'sma', 'symlink', 'absent', 'x', 'h', 'h', 0, 0, 'intent', 0, 'worker', "
              "'2026-10-16T00:00:00+00:00', '2026-10-16T00:00:00+00:00')")
    db = root / "data" / "agentic.db"
    return textwrap.dedent(f"""
        try:
            conn = sqlite3.connect({str(db)!r}, timeout=1)
            conn.execute({insert!r})
            conn.commit()
            record('insert', ['wrote'])
        except (sqlite3.Error, OSError) as exc:
            record('insert', ['denied', type(exc).__name__])
    """)


def _fs_snippet(root: Path, versions: dict) -> str:
    live = root / "plugins" / "sma"
    version_dir = root / "plugins" / ".versions" / "sma" / versions["c_dir"]
    return textwrap.dedent(f"""
        def attempt(label, fn):
            try:
                fn()
                record(label, ['done'])
            except OSError as exc:
                record(label, ['denied', exc.errno])

        def swap():
            os.unlink({str(live)!r})
            os.symlink('../elsewhere', {str(live)!r})

        def replace_via_rename():
            os.symlink('../elsewhere', {str(live) + '.new'!r})
            os.rename({str(live) + '.new'!r}, {str(live)!r})

        def tamper():
            with open({str(version_dir / 'plugin.py')!r}, 'a') as handle:
                handle.write('# tampered')

        def add_version():
            os.mkdir({str(version_dir.parent / ('f' * 64))!r})

        attempt('unlink_and_relink_live', swap)
        attempt('rename_over_live', replace_via_rename)
        attempt('tamper_version_source', tamper)
        attempt('add_version_dir', add_version)
    """)


def _actions(root: Path, versions: dict) -> dict[str, str]:
    yes = ["--yes"]
    return {
        "cli_disable": _cli_snippet(root, ["plugin", "disable", "sma", *yes]),
        "cli_rollback": _cli_snippet(
            root, ["plugin", "rollback", "sma", "--to", versions["b_dir"][:12], *yes]),
        "cli_retire": _cli_snippet(root, ["plugin", "retire", "legacy", *yes]),
        "cli_revoke": _cli_snippet(
            root, ["plugin", "revoke", "sma", "--version", versions["a"][:12], *yes]),
        "db_insert": _db_snippet(root),
        "fs_mutation": _fs_snippet(root, versions),
    }


def _results(stdout: str) -> dict[str, list]:
    found = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            label, outcome = json.loads(line[len("RESULT "):])
            found[label] = outcome
    return found


@pytest.mark.parametrize("action", [
    "cli_disable", "cli_rollback", "cli_retire", "cli_revoke", "db_insert", "fs_mutation"])
def test_isolated_worker_cannot_change_deploy_state(tmp_path, action):
    control_root = tmp_path / "control"
    control_world, control_versions = _build_world(control_root)
    control_before = _tree_state(control_root)
    control = subprocess.run(
        [sys.executable, "-c", _PRELUDE + _actions(control_root, control_versions)[action]],
        capture_output=True, text=True, timeout=120)
    assert control.returncode == 0, control.stderr
    if action.startswith("cli_"):
        assert all(outcome[:2] == ["rc", 0] for outcome in _results(control.stdout).values()), (
            control.stdout)
    assert _tree_state(control_root) != control_before, (
        f"the {action} control did not change anything outside isolation: {control.stdout}")

    root = tmp_path / "deploy"
    _, versions = _build_world(root)
    layout = _mk_repo_layout(tmp_path / "worker")
    before = _tree_state(root)
    result = _run_probe(
        _PRELUDE + _actions(root, versions)[action],
        staging_dir=layout["my_staging"], mission_id="m-001",
        source_snapshot_dir=layout["source_snapshot"], workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    outcomes = _results(result.stdout)
    assert outcomes, f"the worker reported nothing: {result.stdout!r} {result.stderr!r}"

    assert _tree_state(root) == before
    for label, outcome in outcomes.items():
        if outcome[0] == "rc":
            assert outcome[1] != 0, (label, outcome)
            assert outcome[2], f"{label} failed for a reason other than EACCES"
        else:
            assert outcome[0] in {"oserror", "denied"}, (label, outcome)

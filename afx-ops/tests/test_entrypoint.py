"""console script `afx-ops <feature>` の起動口。"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from afx_ops import cli

SCRIPT = Path(sys.executable).parent / "afx-ops"


def _run(args, env, home):
    done = subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                          env=env, cwd=home)
    return done


def test_unknown_feature_exits_nonzero_with_fixed_reason(child_env, home):
    for args in (["nope"], [], ["bot", "extra"]):
        done = _run(args, child_env, home)
        assert done.returncode == 2
        assert "unknown_feature" in done.stderr
        assert done.stdout == ""
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_known_feature_outside_the_unit_stops_at_the_isolation_check(child_env, home, feature):
    # 起動口は隔離の起動順を必ず通る。unit 外 (NoNewPrivs なし) は鍵を読む前に止まる
    done = _run([feature], child_env, home)
    assert done.returncode == 71
    assert done.stderr.strip().splitlines() == ["afx-ops: unit_no_new_privs"]
    assert done.stdout == ""
    # 鍵 dir・設定 dir・状態 dir・socket を作っていない (HOME が空のまま)
    assert list(home.rglob("*")) == []


def test_module_entry_gives_the_same_result(child_env, home):
    done = subprocess.run([sys.executable, "-I", "-m", "afx_ops", "web"], capture_output=True,
                          text=True, timeout=30, env=child_env, cwd=home)
    assert done.returncode == 71
    assert done.stderr.strip().splitlines() == ["afx-ops: unit_no_new_privs"]
    assert list(home.rglob("*")) == []


def test_cli_enters_through_the_fixed_plan():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert {"for_feature", "run"} <= attrs
    assert "_for_tests" not in attrs


def test_cli_body_is_not_implemented_yet():
    assert cli.not_implemented(None) == cli.RC_NOT_IMPLEMENTED

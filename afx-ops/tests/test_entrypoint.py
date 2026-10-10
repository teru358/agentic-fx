"""console script `afx-ops <feature>` の起動口。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

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
def test_known_feature_without_body_exits_nonzero_and_touches_nothing(child_env, home, feature):
    done = _run([feature], child_env, home)
    assert done.returncode == 69
    assert "feature_not_implemented" in done.stderr
    assert done.stdout == ""
    # 鍵 dir・socket を作っていない (HOME が空のまま)。
    assert list(home.rglob("*")) == []


def test_module_entry_gives_the_same_result(child_env, home):
    done = subprocess.run([sys.executable, "-I", "-m", "afx_ops", "web"], capture_output=True,
                          text=True, timeout=30, env=child_env, cwd=home)
    assert done.returncode == 69
    assert "feature_not_implemented" in done.stderr

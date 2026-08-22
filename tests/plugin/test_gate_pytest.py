"""Landlock ゲート pytest (プラン10 Task 6、設計書 §4.2-3d、§8.1-10)。

測定 1 件 = 子プロセス 1 個 (Landlock 不可逆)。A-1 の
`agentic_fx.runners.launcher.build_launcher_argv` に依存する。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.landlock import is_available
from agentic_fx.plugin.gate_pytest import GateResult, run_gate_pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.yaml.example"

_PASSING_TEST = "def test_ok():\n    assert 1 == 1\n"
_FAILING_TEST = "def test_fail():\n    assert 1 == 2\n"


@pytest.fixture(scope="module")
def settings():
    return load_settings(_EXAMPLE)


def _skip_if_no_landlock():
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")


def _write_candidate(tmp_path: Path, *, test_py: str) -> Path:
    d = tmp_path / "candidate"
    d.mkdir()
    (d / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (d / "config.yaml").write_text("kind: indicator\n")
    (d / "test_plugin.py").write_text(test_py)
    return d


def test_run_gate_pytest_passes_for_passing_test(tmp_path, settings):
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)
    result = run_gate_pytest(d, settings=settings)
    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.returncode == 0


def test_run_gate_pytest_fails_for_failing_test(tmp_path, settings):
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=_FAILING_TEST)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is False
    assert result.returncode != 0


def test_run_gate_pytest_candidate_dir_is_read_only(tmp_path, settings):
    """test_plugin.py が自分の候補ディレクトリへ書こうとすると EACCES —
    候補は read-only (§4.2-3e の主 pin)。"""
    _skip_if_no_landlock()
    write_attempt = (
        "from pathlib import Path\n"
        "import pytest\n"
        "def test_write_denied():\n"
        "    with pytest.raises(PermissionError):\n"
        "        (Path(__file__).parent / 'plugin.py').write_text('OWNED')\n")
    d = _write_candidate(tmp_path, test_py=write_attempt)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is True, result.stdout_tail


def test_run_gate_pytest_cannot_open_agentic_db(tmp_path, settings, monkeypatch):
    """ゲート子プロセスから `data/agentic.db` を開こうとすると EACCES
    (§8.1-10)。親が絶対パスを子へ明示的に渡し、それでも開けないことを
    確認する非対称設計 (申し送り⑨) — repo 直下の実 `data/agentic.db`
    ではなく、gate worker が受け取る argv 経由のパスを使う。"""
    _skip_if_no_landlock()
    # data/agentic.db が存在しない場合は作成
    db_dir = _REPO_ROOT / "data"
    db_dir.mkdir(exist_ok=True)
    probe_db = db_dir / "agentic.db"
    probe_db.write_bytes(b"test")
    try:
        check_db_access = (
            "def test_db_is_eacces():\n"
            f"    import pytest\n"
            f"    with pytest.raises((PermissionError, FileNotFoundError)):\n"
            f"        open({str(probe_db)!r}, 'rb')\n")
        d = _write_candidate(tmp_path, test_py=check_db_access)
        result = run_gate_pytest(d, settings=settings)
        assert result.passed is True, result.stdout_tail
    finally:
        if probe_db.exists():
            probe_db.unlink()
        if db_dir.exists() and not any(db_dir.iterdir()):
            db_dir.rmdir()


def test_run_gate_pytest_asserts_pycache_prefix(tmp_path, settings):
    """gate worker が起動直後に `sys.pycache_prefix` を assert する —
    `PYTHONPYCACHEPREFIX` を Popen env に置かない変異は red になる。"""
    _skip_if_no_landlock()
    check_pycache = (
        "import sys\n"
        "def test_pycache_prefix_is_set():\n"
        "    assert sys.pycache_prefix is not None\n")
    d = _write_candidate(tmp_path, test_py=check_pycache)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is True, result.stdout_tail


def test_run_gate_pytest_fails_closed_when_landlock_unavailable(tmp_path, settings, monkeypatch):
    import agentic_fx.plugin.gate_pytest as gate_mod
    monkeypatch.setattr(gate_mod, "is_available", lambda: False)
    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)
    with pytest.raises(RuntimeError, match="[Ll]andlock"):
        run_gate_pytest(d, settings=settings)


def test_run_gate_pytest_times_out(tmp_path, settings):
    _skip_if_no_landlock()
    from agentic_fx.config import PluginSettings
    short_timeout_settings = settings.model_copy(
        update={"plugin": settings.plugin.model_copy(
            update={"pytest_timeout_sec": 0.5})})
    slow_test = "import time\ndef test_slow():\n    time.sleep(5)\n"
    d = _write_candidate(tmp_path, test_py=slow_test)
    result = run_gate_pytest(d, settings=short_timeout_settings)
    assert result.passed is False


def test_gate_pytest_does_not_use_preexec_fn():
    """M6 静的 pin: preexec_fn を使っていないこと。"""
    import inspect
    from agentic_fx.plugin import gate_pytest
    src = inspect.getsource(gate_pytest)
    assert "preexec_fn" not in src

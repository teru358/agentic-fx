"""実資源に触れないためのガード。

- 実 home の鍵 dir (`~/.config/agentic-fx`)、repo の `data/`・`.env` を session の前後で比べ、
  変化があれば落とす。
- 各テストの HOME / XDG_CONFIG_HOME は tmp_path に向ける。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AFX_OPS = Path(__file__).resolve().parents[1]
_REAL_HOME = Path.home()


def _signature(path: Path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_dir() and not path.is_symlink():
        entries = []
        for child in sorted(path.rglob("*")):
            try:
                cst = child.lstat()
            except FileNotFoundError:
                continue
            entries.append((str(child), cst.st_mtime_ns, cst.st_size))
        return ("dir", st.st_mtime_ns, tuple(entries))
    return ("file", st.st_mtime_ns, st.st_size)


_GUARDED = (REPO / ".env", REPO / "data", _REAL_HOME / ".config" / "agentic-fx")


@pytest.fixture(autouse=True, scope="session")
def _guard_real_keys_data_and_env_are_untouched():
    before = {path: _signature(path) for path in _GUARDED}
    yield
    after = {path: _signature(path) for path in _GUARDED}
    changed = [str(path) for path in _GUARDED if before[path] != after[path]]
    assert not changed, f"afx-ops tests touched real resources: {changed}"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(path / ".config"))
    return path


@pytest.fixture
def child_env(home) -> dict[str, str]:
    """子 process 用の環境。実 HOME を渡さない。"""
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config")}

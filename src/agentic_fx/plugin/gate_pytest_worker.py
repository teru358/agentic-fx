"""Landlock ゲート pytest の子プロセスエントリ (プラン10 Task 6、
設計書 §4.2-3d)。共通 launcher (`agentic_fx.runners.launcher`) から
`python -m agentic_fx.plugin.gate_pytest_worker <plugin_dir> <workdir>`
として exec される。handshake を持たない — 親から argv だけを受け取る。
"""
from __future__ import annotations

import sys

assert sys.pycache_prefix is not None, (
    "PYTHONPYCACHEPREFIX must be set in the child env before interpreter "
    "startup (設計書 §4.2-3d, codex 2 周目 M1)")

import sysconfig
from pathlib import Path

from agentic_fx.core import landlock


def main() -> None:
    plugin_dir = Path(sys.argv[1]).resolve()
    workdir = Path(sys.argv[2]).resolve()

    code_root = Path(__file__).resolve().parents[1]
    venv_root = Path(sys.prefix).resolve()
    stdlib_root = Path(sysconfig.get_paths()["stdlib"]).resolve()
    base_prefix = Path(sys.base_prefix).resolve()

    read_only = [code_root, venv_root, stdlib_root]
    if base_prefix != venv_root:
        read_only.append(base_prefix)
    for p in (Path("/usr/lib"), Path("/usr/share/zoneinfo"), Path("/etc"), Path("/tmp")):
        if p.exists():
            read_only.append(p)

    # プラン10 Task 5 5-C 改訂 (2026-08-22, 裁定 A): ディレクトリ単位の
    # EXECUTE 付与から /usr/lib/usr/lib64 を外し、
    # landlock.interpreter_files_for で ELF interpreter 実体ファイルのみを
    # execute_file_paths として渡す。
    execute_file_paths = landlock.interpreter_files_for([sys.executable])
    if base_prefix != venv_root:
        python_at_base = Path(base_prefix) / "bin" / "python3"
        if python_at_base.is_file():
            execute_file_paths.extend(
                landlock.interpreter_files_for([python_at_base]))

    landlock.restrict_to(read_only_paths=read_only,
                         read_write_paths=[workdir, plugin_dir, Path("/dev")],
                         execute_file_paths=execute_file_paths)

    import pytest
    rc = pytest.main(["-q", "-p", "no:logging", "-p", "no:cacheprovider",
                      "--rootdir", str(workdir), str(plugin_dir)])
    raise SystemExit(int(rc))


if __name__ == "__main__":
    main()

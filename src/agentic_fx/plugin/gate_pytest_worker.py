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

    read_only = [code_root, venv_root, stdlib_root, plugin_dir]
    if base_prefix != venv_root:
        read_only.append(base_prefix)
    for p in (Path("/usr/lib"), Path("/usr/share/zoneinfo"), Path("/etc")):
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
                         read_write_paths=[workdir, Path("/dev")],
                         execute_file_paths=execute_file_paths)

    import pytest
    # `-c <workdir>/pytest.ini`: implicit inifile 探索 (親ディレクトリを
    # 辿って pyproject.toml/setup.cfg/tox.ini を探す) を止める — 親
    # (gate_pytest.run_gate_pytest) が workdir に空 ini を用意済み。
    # `--basetemp`: tmp_path 系フィクスチャや pytest 内部の一時領域を
    # workdir 配下に固定し、/tmp への到達を発生させない (/tmp は
    # allowlist に含めない — 前任の逸脱を撤回)。
    # `--confcutdir <plugin_dir>`: `-c` で inifile を workdir 配下に固定
    # したことで pytest の既定 confcutdir 算出 (`inipath.parent`、つまり
    # workdir) が plugin_dir の祖先と無関係な木になり、`Session.collect`
    # (`_pytest/main.py` の `_is_in_confcutdir`) が plugin_dir の祖先
    # ディレクトリ (`/home` 等、allowlist 外) を辿って `Dir` collector を
    # 作ろうとし EACCES になる (実測で確認済み — 2026-08-22 検収)。
    # confcutdir を plugin_dir 自身に固定することで、この祖先ディレクトリ
    # 探索を argpath の直上で打ち切らせる。
    rc = pytest.main(["-q", "-p", "no:logging", "-p", "no:cacheprovider",
                      "--rootdir", str(workdir),
                      "-c", str(workdir / "pytest.ini"),
                      "--confcutdir", str(plugin_dir),
                      "--basetemp", str(workdir / "basetemp"),
                      str(plugin_dir)])
    raise SystemExit(int(rc))


if __name__ == "__main__":
    main()

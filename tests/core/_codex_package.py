"""codex の package 配置 (`bin/codex`、`bin/codex-code-mode-host`、`codex-package.json`) を
一時 dir に作る。副作用なしの module (実隔離の service helper と pytest の両方から使う)。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE_PY = os.path.realpath(sys.executable)


def _package_base() -> str:
    """BASE_PY と同じ fs の dir (hard link を張れる場所)。tempdir が別 fs ならリポジトリの tmp/。"""
    tmp = tempfile.gettempdir()
    if os.stat(tmp).st_dev == os.stat(BASE_PY).st_dev:
        return tmp
    base = REPO / "tmp"
    base.mkdir(exist_ok=True)
    return str(base)


def make_codex_package(*, copy_cli: bool = False, root: str | None = None) -> Path:
    """codex の package 配置 (`bin/codex`、`bin/codex-code-mode-host`、`codex-package.json`) を
    作って `bin/codex` を返す。host は BASE_PY の hard link (別 fs なら copy)。`bin/codex` も hard
    link だが、中身を書き換える試験は `copy_cli=True` で独立した file にする。package の dir は
    呼び出し側が消す (`bin/codex` の 2 つ上)。"""
    pkg = Path(tempfile.mkdtemp(prefix="afx-codexpkg-", dir=root or _package_base()))
    bindir = pkg / "bin"
    bindir.mkdir()

    def place(dst: Path, copy: bool) -> None:
        if not copy:
            try:
                os.link(BASE_PY, dst)
                return
            except OSError:
                pass
        shutil.copy2(BASE_PY, dst)

    place(bindir / "codex", copy_cli)
    place(bindir / "codex-code-mode-host", False)
    (pkg / "codex-package.json").write_text('{"layoutVersion":1,"entrypoint":"bin/codex"}')
    return bindir / "codex"

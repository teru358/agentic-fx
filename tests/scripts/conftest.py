from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

import pytest

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REPOSITORY = Path(__file__).resolve().parents[2]


def _under(path: str | None, base: Path) -> bool:
    if not path:
        return False
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(base))
    except ValueError:
        return False
    return True


def _invokes_reset(arguments: object) -> bool:
    if isinstance(arguments, (str, bytes, os.PathLike)):
        arguments = [arguments]
    try:
        return any(Path(os.fsdecode(argument)).name == "first-run-reset.sh" for argument in arguments)  # type: ignore[union-attr]
    except TypeError:
        return False


def _data_signature():
    try:
        status = (REPOSITORY / "data").lstat()
    except FileNotFoundError:
        return None
    return (status.st_ino, status.st_mtime_ns)


@pytest.fixture(autouse=True, scope="session")
def _guard_reset_script_never_touches_real_home_or_data():
    """リセット script を実 home の設定領域や実 repo の data/ に向けて起動しない。

    script を起動する時点の環境 (呼び出し側が env を渡さなければ現在の環境) で
    HOME が実 home のまま、または XDG_CONFIG_HOME が実 home 配下を指していれば
    違反として記録し、session の終わりに落とす。あわせて実 repo の data/ が
    session の前後で変わっていないことを確かめる。"""
    violations: list[str] = []
    original_init = subprocess.Popen.__init__

    def checked_init(self, args, *positional, **keywords):
        if _invokes_reset(args):
            environment = keywords.get("env")
            if environment is None:
                environment = os.environ
            home = environment.get("HOME")
            xdg = environment.get("XDG_CONFIG_HOME")
            if not home or os.path.realpath(home) == os.path.realpath(REAL_HOME):
                violations.append(f"HOME={home!r} が実 home を指したまま起動された")
            if xdg and os.path.isabs(xdg) and _under(xdg, REAL_HOME):
                violations.append(f"XDG_CONFIG_HOME={xdg!r} が実 home 配下を指したまま起動された")
        original_init(self, args, *positional, **keywords)

    before = _data_signature()
    subprocess.Popen.__init__ = checked_init  # type: ignore[method-assign]
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original_init  # type: ignore[method-assign]
    after = _data_signature()
    failures = list(violations)
    if before != after:
        failures.append(f"実 {REPOSITORY / 'data'} がテスト実行中に変更された: {before} -> {after}")
    if failures:
        pytest.fail("\n".join(failures), pytrace=False)

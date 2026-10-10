"""ops template と afx の Wants drop-in を冪等に配置する。"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path


_FEATURES = frozenset({"bot", "web", "notify"})
# 試験で探索結果を差し替えられるよう、module の名前として持つ。
_which = shutil.which
_find_spec = importlib.util.find_spec


class OpsExecNotFound(LookupError):
    """afx-ops の起動コマンドを、unit に書いてよい絶対 path に解決できない。message は理由。"""


_MODULE_SUFFIX = " -m afx_ops"
# 引用符・\ は systemd の引用と escape、% は specifier、$ は環境変数展開として解釈される。
_QUOTES = frozenset("\"'")


@dataclass(frozen=True)
class UnitPaths:
    unit_dir: Path
    afx_unit: str = "afx.service"
    # 試験が本物の unit 名と衝突しない disposable unit を作れるよう、名前と起動コマンドを差し替え可能にする。
    ops_prefix: str = "afx-ops"
    # None は「install 時に探す」。systemd は ExecStart の裸の名前を利用者の PATH で探さない。
    ops_exec: str | None = None

    @property
    def template_path(self) -> Path:
        return self.unit_dir / f"{self.ops_prefix}@.service"

    def feature_unit(self, feature: str) -> str:
        return f"{self.ops_prefix}@{feature}.service"

    @property
    def dropin_path(self) -> Path:
        return self.unit_dir / f"{self.afx_unit}.d" / "afx-ops.conf"


def _template_text() -> str:
    # 配布後も source checkout に依存せず読めるよう、package data として同梱する。
    return resources.files(__package__).joinpath("afx-ops@.service.in").read_text()


def fsync_dir(directory: Path) -> None:
    """dir の entry 変更 (rename・unlink) を durable にする。"""
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def remove_durably(path: Path) -> None:
    """file を消し、親 dir を fsync する。無ければ何もしない。"""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_dir(path.parent)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """一意な一時名 (O_EXCL|O_NOFOLLOW) に書いて fsync し、rename で置き換える。

    予測可能な一時名だと、親 dir に先置きされた symlink を辿って別の file を書き換え得る。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_if_changed(path: Path, content: str) -> None:
    encoded = content.encode()
    try:
        if path.read_bytes() == encoded:
            return
    except FileNotFoundError:
        pass
    atomic_write(path, encoded, 0o644)


def _syntax_problem(executable: str) -> str:
    """ExecStart に引用なしで書けない path なら理由を返す。"""
    if not os.path.isabs(executable):
        return "絶対 path でない"
    for char in executable:
        if char.isspace():
            return "空白・改行を含む"
        if char in _QUOTES:
            return "引用符を含む"
        if char in "%$\\":
            return f"{char} を含む"
        if not char.isprintable():
            return "制御文字を含む"
    return ""


def _executable_problem(executable: str) -> str:
    """unit に書いて systemd に実行させてよい file でなければ理由を返す。

    所有者と書込み権限は symlink を辿った実体で見る。group 書込みは、利用者 private group が
    普通の環境 (uv の python は 775) で正当な実行ファイルを弾くので見ない。
    """
    problem = _syntax_problem(executable)
    if problem:
        return problem
    try:
        info = os.stat(executable)
    except OSError as error:
        return f"stat できない ({type(error).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return "通常 file でない"
    # root 所有 (システムの python や配布物) は利用者より強い持ち主なので許す。
    if info.st_uid not in (os.getuid(), 0):
        return f"所有者が自 uid でも root でもない (uid={info.st_uid})"
    if info.st_mode & stat.S_IWOTH:
        return f"他者が書き込める (mode={stat.S_IMODE(info.st_mode):o})"
    if not os.access(executable, os.X_OK):
        return "実行できない"
    return ""


def _split_exec(command: str) -> str:
    """起動コマンドから実行ファイルを取り出す。許す形は path 単独か `path -m afx_ops` だけ。"""
    return command[:-len(_MODULE_SUFFIX)] if command.endswith(_MODULE_SUFFIX) else command


def _checked(command: str, executable: str) -> str:
    problem = _executable_problem(executable)
    if problem:
        raise OpsExecNotFound(f"path={executable!r} {problem}")
    return command


def resolve_ops_exec(paths: UnitPaths) -> UnitPaths:
    """ExecStart に書く起動コマンドを絶対 path で決め、unit に書いてよいかを確かめる。

    PATH 上の afx-ops を優先し、無ければ今の python で afx_ops module を起動する。どちらも
    (与えられたコマンドも) 実体が通常 file・自 uid か root の持ち物・他者書込み不可・実行可であることを
    確かめる。汚染された PATH から拾った file を永続 unit に書かないためである。

    systemd の ExecStart は shell と違う引用・escape・specifier (%)・変数展開 ($) の規則を持つ。
    escape して書く代わりに、それらの文字を含む path は理由付きで拒否する。PATH 上の afx-ops が
    拒否されても module 起動へ黙って逃げない (汚染を見落とさないため)。
    """
    if paths.ops_exec is not None:
        _checked(paths.ops_exec, _split_exec(paths.ops_exec))
        return paths
    found = _which("afx-ops")
    if found:
        path = os.path.abspath(found)
        return replace(paths, ops_exec=_checked(path, path))
    if _find_spec("afx_ops") is not None:
        command = f"{sys.executable}{_MODULE_SUFFIX}"
        return replace(paths, ops_exec=_checked(command, sys.executable))
    raise OpsExecNotFound("afx-ops が PATH に無く、afx_ops module も import できない")


def _rendered(paths: UnitPaths, features: set[str]) -> tuple[str, str]:
    if paths.ops_exec is None or _syntax_problem(_split_exec(paths.ops_exec)):
        raise OpsExecNotFound(f"ops_exec={paths.ops_exec!r} unit に書けない")
    template = _template_text().replace("@AFX_UNIT@", paths.afx_unit).replace(
        "@AFX_OPS_EXEC@", paths.ops_exec)
    wants = " ".join(paths.feature_unit(feature) for feature in sorted(features))
    return template, f"[Unit]\nWants={wants}\n"


def install_units(paths: UnitPaths, features: set[str]) -> None:
    """template と有効 feature 全体の Wants を exact にそろえる。"""
    if not features or not features <= _FEATURES:
        raise ValueError("unknown_feature")
    template, dropin = _rendered(paths, features)
    _write_if_changed(paths.template_path, template)
    _write_if_changed(paths.dropin_path, dropin)


def units_match(paths: UnitPaths, features: set[str]) -> bool:
    """設置済みの template と drop-in が、features に対して書くはずの内容と一致するか。"""
    template, dropin = _rendered(paths, features)
    try:
        return (paths.template_path.read_text() == template
                and paths.dropin_path.read_text() == dropin)
    except (OSError, UnicodeDecodeError):
        # dir に化けた・権限が無い等で読めない資材も「書くはずの内容と違う」とみなす。
        return False


def uninstall_feature(paths: UnitPaths, remaining_features: set[str]) -> None:
    """対象 feature を Wants 集合から外し、最後なら資材全体を除去する。"""
    if not remaining_features:
        remove_durably(paths.dropin_path)
        remove_durably(paths.template_path)
        return
    install_units(paths, remaining_features)

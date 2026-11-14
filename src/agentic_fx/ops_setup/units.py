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
BOT_DROPIN_NAME = "bot-recovery.conf"
# 恒久の終了 (unit / argv 不正・key 不正・未実装・runtime import・共通 startup 失敗・bot の恒久判断) は
# 再起動で直らないので、無期限の再起動から外す。値は bot の exit 表と一致させる。
BOT_PERMANENT_EXIT_STATUS = (2, 4, 69, 70, 71, 78)
# RestartSteps / RestartMaxDelaySec は systemd 254 で入った。
BOT_MIN_SYSTEMD_VERSION = 254
BOT_DROPIN = (
    "[Unit]\nStartLimitIntervalSec=0\n\n"
    "[Service]\nRestart=on-failure\nRestartSec=30s\nRestartSteps=5\nRestartMaxDelaySec=10m\n"
    "RestartPreventExitStatus=" + " ".join(str(code) for code in BOT_PERMANENT_EXIT_STATUS) + "\n")
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

    @property
    def bot_dropin_path(self) -> Path:
        """bot instance だけに効く recovery の drop-in。template 全体の drop-in にはしない。"""
        return self.unit_dir / f"{self.feature_unit('bot')}.d" / BOT_DROPIN_NAME

    def artifact_paths(self, features: set[str]) -> list[Path]:
        """features に対して install が置く資材の全 path。"""
        paths = [self.template_path, self.dropin_path]
        if "bot" in features:
            paths.append(self.bot_dropin_path)
        return paths


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


def _syntax_problem(executable: str, *, forbidden: str = "") -> str:
    """ExecStart に引用なしで書けない path なら理由を返す。

    `forbidden` はその書き場所だけで書けない文字 (PATH の区切りの ":" など)。"""
    if not os.path.isabs(executable):
        return "絶対 path でない"
    for char in executable:
        if char.isspace():
            return "空白・改行を含む"
        if char in _QUOTES:
            return "引用符を含む"
        if char in "%$\\" or char in forbidden:
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


def bot_systemd_problem(version: int | None) -> str:
    """bot の recovery drop-in を使えない systemd なら固定の理由を返す。使えるなら空文字。

    古い systemd で drop-in の一部だけが効くと、無期限の固定間隔再起動になり得る。
    暗黙の fallback は作らず、bot の enable を拒否する。"""
    if version is None:
        return "systemd の version を確認できない"
    if version < BOT_MIN_SYSTEMD_VERSION:
        return f"systemd {version} は {BOT_MIN_SYSTEMD_VERSION} 未満 (RestartSteps が使えない)"
    return ""


def _rendered(paths: UnitPaths, features: set[str]) -> tuple[str, str]:
    if paths.ops_exec is None or _syntax_problem(_split_exec(paths.ops_exec)):
        raise OpsExecNotFound(f"ops_exec={paths.ops_exec!r} unit に書けない")
    template = _template_text().replace("@AFX_UNIT@", paths.afx_unit).replace(
        "@AFX_OPS_EXEC@", paths.ops_exec)
    wants = " ".join(paths.feature_unit(feature) for feature in sorted(features))
    return template, f"[Unit]\nWants={wants}\n"


def install_units(paths: UnitPaths, features: set[str], *, write_bot_dropin: bool = True) -> None:
    """template と有効 feature 全体の Wants を exact にそろえる。

    bot の drop-in を書いてよいのは、systemd の版の検査を通った enable だけである。それ以外
    (disable・rollback) は `write_bot_dropin=False` で呼び、bot が残る集合でも drop-in を
    新しく作らず修復もしない。bot が集合に無ければ、残っている drop-in は取り除く。"""
    if not features or not features <= _FEATURES:
        raise ValueError("unknown_feature")
    template, dropin = _rendered(paths, features)
    _write_if_changed(paths.template_path, template)
    _write_if_changed(paths.dropin_path, dropin)
    if "bot" not in features:
        _remove_bot_dropin(paths)
    elif write_bot_dropin:
        _write_if_changed(paths.bot_dropin_path, BOT_DROPIN)


def bot_dropin_exact(paths: UnitPaths) -> bool:
    """bot の drop-in が書くはずの内容と exact に一致するか。読めなければ False。"""
    try:
        return paths.bot_dropin_path.read_text() == BOT_DROPIN
    except (OSError, UnicodeDecodeError):
        return False


def _remove_bot_dropin(paths: UnitPaths) -> None:
    remove_durably(paths.bot_dropin_path)
    try:
        paths.bot_dropin_path.parent.rmdir()
    except OSError:
        # 無い・他の file が残っている dir は触らない。
        return
    fsync_dir(paths.unit_dir)


def units_match(paths: UnitPaths, features: set[str], *, check_bot_dropin: bool = True) -> bool:
    """設置済みの template と drop-in が、features に対して書くはずの内容と一致するか。

    `check_bot_dropin=False` は、bot が残る集合で bot の drop-in の中身を見ない
    (disable は bot の drop-in を直さないので、その食い違いで止まらない)。bot が無い集合では
    drop-in が残っていないことを常に見る。"""
    template, dropin = _rendered(paths, features)
    try:
        if "bot" in features:
            if check_bot_dropin and paths.bot_dropin_path.read_text() != BOT_DROPIN:
                return False
        elif os.path.lexists(paths.bot_dropin_path):
            return False
        return (paths.template_path.read_text() == template
                and paths.dropin_path.read_text() == dropin)
    except (OSError, UnicodeDecodeError):
        # dir に化けた・権限が無い等で読めない資材も「書くはずの内容と違う」とみなす。
        return False


def uninstall_feature(paths: UnitPaths, remaining_features: set[str]) -> None:
    """対象 feature を Wants 集合から外し、最後なら資材全体を除去する。"""
    if not remaining_features:
        _remove_bot_dropin(paths)
        remove_durably(paths.dropin_path)
        remove_durably(paths.template_path)
        return
    install_units(paths, remaining_features, write_bot_dropin=False)

"""plugin の 3 ファイルを安全に読む共通の opener。

承認 payload・ジャーナル・archive・live の symlink が持つ path 文字列は永続化された値
であり、壊れていれば任意のローカルファイルを読む実装になり得る。そこで path 文字列は
開かず、検証済みの値 (plugin 名・64 桁 hex・数値の mission id) から root 配下の
component を自分で組み立て、root から 1 component ずつ ``openat`` + ``O_NOFOLLOW`` で
辿る。directory と 3 ファイルの性質・サイズ・hash の検査も同じ中で済ませ、検査を通る前
の bytes は呼び出し側へ出さない。

失敗は固定の reason code だけを持つ :class:`SourceError` で返す。解決した絶対パスは
例外にもログにも入れない。
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from agentic_fx.plugin import version_store

from .change_limits import (HASH_MISMATCH, MAX_FILE_BYTES, PLUGIN_FILES, TOO_LARGE,
                            UNAVAILABLE, UNSAFE_SOURCE)

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
MISSION_ID_MAX = 10 ** 18

VERSION_ROOT = ".versions"
STAGING_ROOT = "_staging"
ARCHIVE_ROOT = "_archive"
HUMAN_ROOT = "_human"

# 改善レポート (親専有の `data/improve_reports`) を読む上限と名前の文法。
REPORT_MAX_BYTES = 64 * 1024
_REPORT_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 64 * 1024


class SourceError(Exception):
    """読めなかった理由。``code`` は ``change_limits`` の固定 reason code。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Source:
    """root 名と、その下を順に辿る component。構築時に値を検証済み。"""

    root: str
    components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceFiles:
    plugin_py: bytes
    config_yaml: bytes
    test_plugin: bytes


@dataclass(frozen=True, slots=True)
class LiveEntry:
    kind: str  # absent | symlink | dir | other
    target: str | None = None


def _name(value: object) -> str:
    if type(value) is not str or NAME_RE.fullmatch(value) is None:
        raise SourceError(UNSAFE_SOURCE)
    return value


def _hash(value: object) -> str:
    if type(value) is not str or HASH_RE.fullmatch(value) is None:
        raise SourceError(UNSAFE_SOURCE)
    return value


def _mission(value: object) -> str:
    if type(value) is not int or not 1 <= value < MISSION_ID_MAX:
        raise SourceError(UNSAFE_SOURCE)
    return str(value)


def version_source(name: object, artifact_hash: object) -> Source:
    return Source(VERSION_ROOT, (_name(name), _hash(artifact_hash)))


def staging_source(mission_id: object, name: object) -> Source:
    return Source(STAGING_ROOT, (_mission(mission_id), _name(name)))


def archive_source(mission_id: object, artifact_hash: object) -> Source:
    return Source(ARCHIVE_ROOT, (_mission(mission_id), _hash(artifact_hash)))


def human_source(name: object) -> Source:
    return Source(HUMAN_ROOT, (_name(name),))


def baseline_hash_from_target(name: object, target: object) -> str | None:
    """live の symlink target / journal の ``old_target`` を ``.versions/<name>/<64 桁 hex>``
    との完全一致で解釈し、64 桁 hex を取り出す。一致しなければ None。"""
    if type(name) is not str or type(target) is not str:
        return None
    if NAME_RE.fullmatch(name) is None:
        return None
    prefix = f"{VERSION_ROOT}/{name}/"
    if not target.startswith(prefix):
        return None
    tail = target[len(prefix):]
    return tail if HASH_RE.fullmatch(tail) is not None else None


def _open_dir(parent_fd: int, name: str) -> int:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise SourceError(UNAVAILABLE) from None
    except OSError:
        raise SourceError(UNSAFE_SOURCE) from None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise SourceError(UNSAFE_SOURCE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_plugins_dir(plugins_root: Path) -> int:
    try:
        fd = os.open(os.fspath(plugins_root), _DIR_FLAGS)
    except FileNotFoundError:
        raise SourceError(UNAVAILABLE) from None
    except OSError:
        raise SourceError(UNSAFE_SOURCE) from None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise SourceError(UNSAFE_SOURCE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(dir_fd: int, filename: str, limit: int = MAX_FILE_BYTES) -> bytes:
    try:
        fd = os.open(filename, _FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise SourceError(UNAVAILABLE) from None
    except OSError:
        raise SourceError(UNSAFE_SOURCE) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SourceError(UNSAFE_SOURCE)
        if info.st_size > limit:
            raise SourceError(TOO_LARGE)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_CHUNK, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise SourceError(TOO_LARGE)
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise SourceError(UNSAFE_SOURCE) from None
    finally:
        os.close(fd)


def read_source(plugins_root: Path, source: Source, expected_artifact_hash: object) -> SourceFiles:
    """root から component を辿って 3 ファイルを読み、再計算した ``artifact_hash`` が
    ``expected_artifact_hash`` と一致したときだけ返す。"""
    expected = _hash(expected_artifact_hash)
    opened: list[int] = []
    try:
        opened.append(_open_plugins_dir(plugins_root))
        for component in (source.root, *source.components):
            opened.append(_open_dir(opened[-1], component))
        leaf = opened[-1]
        files = [_read_file(leaf, filename) for filename in PLUGIN_FILES]
    finally:
        for fd in reversed(opened):
            os.close(fd)
    if version_store.artifact_hash_bytes(*files) != expected:
        raise SourceError(HASH_MISMATCH)
    return SourceFiles(*files)


def inspect_live(plugins_root: Path, name: object) -> LiveEntry:
    """``plugins/<name>`` を追従せずに種別だけ見る。symlink なら target 文字列も返す。"""
    name = _name(name)
    pfd = _open_plugins_dir(plugins_root)
    try:
        try:
            info = os.lstat(name, dir_fd=pfd)
        except FileNotFoundError:
            return LiveEntry("absent")
        except OSError:
            raise SourceError(UNSAFE_SOURCE) from None
        if stat.S_ISLNK(info.st_mode):
            try:
                return LiveEntry("symlink", os.readlink(name, dir_fd=pfd))
            except OSError:
                raise SourceError(UNSAFE_SOURCE) from None
        if stat.S_ISDIR(info.st_mode):
            return LiveEntry("dir")
        return LiveEntry("other")
    finally:
        os.close(pfd)


def read_improve_report(reports_root: Path, mission_id: int,
                        report_path_value: str) -> bytes:
    """親が信頼する ``reports_root`` を dirfd として開き、同一 mission の report 1 本を読む。

    ``report_path_value`` (DB に残る path 文字列) は開かない。basename が
    ``improve-<日付>-<mission_id>.md`` の正規形に一致し、directory 部があれば
    ``reports_root`` と一致するときだけ、``reports_root`` 直下のその名前を
    ``O_NOFOLLOW`` で開く。regular file・64 KiB 以下・厳密な UTF-8 を確かめ、
    失敗は固定の reason code の :class:`SourceError` で返す (path は入れない)。"""
    if type(mission_id) is not int or not 1 <= mission_id < MISSION_ID_MAX:
        raise SourceError(UNSAFE_SOURCE)
    if type(report_path_value) is not str or "\x00" in report_path_value:
        raise SourceError(UNSAFE_SOURCE)
    directory, separator, basename = report_path_value.rpartition("/")
    if re.fullmatch(rf"improve-{_REPORT_DATE}-{mission_id}\.md", basename) is None:
        raise SourceError(UNSAFE_SOURCE)
    if separator and os.path.normpath(directory or "/") != os.path.normpath(
            os.fspath(reports_root)):
        raise SourceError(UNSAFE_SOURCE)
    dir_fd = _open_plugins_dir(reports_root)
    try:
        data = _read_file(dir_fd, basename, REPORT_MAX_BYTES)
    finally:
        os.close(dir_fd)
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SourceError(UNSAFE_SOURCE) from None
    return data

"""activity ログ (カテゴリ軸)。ユーザーが読む行動記録 — 設計書 §13。"""
from __future__ import annotations

import errno
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from agentic_fx._safe_error import safe_error_text

_log = logging.getLogger("agentic_fx.activity")


class Category(StrEnum):
    NEWS = "NEWS"
    TECH = "TECH"
    AGGREGATE = "AGGREGATE"
    TRADE = "TRADE"
    IMPROVE = "IMPROVE"
    APPROVAL = "APPROVAL"
    SYSTEM = "SYSTEM"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "activity log write made no progress")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ActivityLog:
    def __init__(self, path: Path, *,
                 on_write_failure: Callable[[Exception], None] | None = None) -> None:
        self._path = path
        self._on_write_failure = on_write_failure
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, category: Category | str, event: str, summary: str,
              ref_id: str | None = None) -> None:
        """activity 行を追記する。

        契約 (N2 再レビューで確定): **write は例外を送出しない**。activity
        は可観測性の記録であり、資金保護経路 (scheduler の隔離ハンドラ群 —
        mark_to_market_error / limit_fill_error / exit_check_error 等) の
        あらゆる場所から呼ばれるため、呼び出し側で毎回 try に包んで防ぐ
        方針は site 単位のモグラ叩きになる (N1 で 1 箇所塞いだ直後に別の
        呼び出し site で同型の欠陥 N2 が実測された)。発生源であるここで
        一度だけ塞ぐ — 書き込みに失敗しても記録漏れとして技術ログに
        warning を残すのみで、呼び出し元には決して伝播させない。

        I3 (fix round 1): 整形 3 行 (ts / clean / line の組み立て) も
        try の中に含める。以前は try の外にあり、``summary=None``
        (``None.split()``) や ``category`` が ``Category`` ではなく素の
        str (``str.value`` は存在しない) のケースで「決して送出しない」
        契約に反して例外が伝播していた (レビュアー実測)。
        """
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            clean = " ".join(summary.split())
            cat_value = Category(category).value
            line = "\t".join([ts, cat_value, event, clean, ref_id or "-"])
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 — write は送出しない契約
            # category が Category でない場合に備え、ログ用表示は
            # `.value` に依存しない (getattr で素通しできればそれを使う)。
            cat_repr = getattr(category, "value", category)
            _log.warning("activity write failed (%s/%s): %s",
                         cat_repr, event, safe_error_text(e))
            if self._on_write_failure is not None:
                try:
                    self._on_write_failure(e)
                except Exception:  # noqa: BLE001 — write remains non-throwing
                    _log.exception("activity write failure callback failed")

    def write_required(self, category: Category | str, event: str, summary: str,
                       ref_id: str | None = None) -> None:
        """監査記録用の追記。`write` と違い、書けなければ例外を送出する。

        書けなかった記録を前提に次の操作へ進まないための入口で、追記後に fsync する。
        前回の書込み中に落ちて末尾が改行で終わっていなければ、先に改行を足すので、
        既に残っている部分行にこの行が連結されることはない。書込みは追記だけで、他の
        行の byte は上書きしない。書き手どうしは排他しないため、改行の要否を見た直後に
        別の書き手が追記すると、次のどちらかになる。完全な行なら、それは前の部分行に
        連結されたまま残り (通常の `write` が部分行の後に書く場合と同じ)、この行の前に
        空行が 1 行入る。その書き手が書込み中に落ちて部分行を残したなら、この行はその
        部分行に連結される。
        ファイルを新しく作ったときは、その directory と親 directory も fsync する。
        """
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        clean = " ".join(summary.split())
        line = "\t".join([ts, Category(category).value, event, clean, ref_id or "-"])
        fd, created = self._open_for_append()
        try:
            data = (("\n" if self._needs_newline(fd) else "") + line + "\n").encode("utf-8")
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            _fsync_dir(self._path.parent)
            _fsync_dir(self._path.parent.parent)

    def _open_for_append(self) -> tuple[int, bool]:
        """追記用に開き、このプロセスがファイルを作ったかを返す。

        作成は `O_EXCL` でだけ行い、既存なら `O_CREAT` なしで開く。その間に消されたら
        作成からやり直すので、作ったのに作っていないと判定することはない。
        """
        flags = os.O_RDWR | os.O_APPEND
        while True:
            try:
                return os.open(self._path, flags | os.O_CREAT | os.O_EXCL, 0o644), True
            except FileExistsError:
                pass
            try:
                return os.open(self._path, flags), False
            except FileNotFoundError:
                continue

    @staticmethod
    def _needs_newline(fd: int) -> bool:
        size = os.fstat(fd).st_size
        return size > 0 and os.pread(fd, 1, size - 1) != b"\n"

    def contains(self, event: str, ref_id: str) -> bool:
        """Whether a line with this event and ref_id is already in the log.

        A missing file is simply "no"; any other read failure is raised so the
        caller can tell "absent" from "could not check".
        """
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return False
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) >= 5 and fields[2] == event and fields[4] == ref_id:
                return True
        return False

    def end_partial_line(self) -> None:
        """Terminate a last line left without its newline (a torn append).

        Without this the next appended line would join the torn one.  A missing
        or empty file needs nothing; other failures are raised.  The newline is
        appended (never written at a remembered offset), so no other line's bytes
        are overwritten.  Writers do not lock each other: a line another writer
        appends between the check and this append stays joined to the torn line,
        followed by one blank line.
        """
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_APPEND)
        except FileNotFoundError:
            return
        try:
            if self._needs_newline(fd):
                _write_all(fd, b"\n")
        finally:
            os.close(fd)

    def tail(self, n: int = 20, category: Category | None = None) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if category is not None:
            lines = [l for l in lines
                     if l.split("\t", 2)[1:2] == [category.value]]
        return lines[-n:]

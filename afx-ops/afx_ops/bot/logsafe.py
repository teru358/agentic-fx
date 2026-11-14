"""bot の log と asyncio の例外を、固定語と件数だけの出力に閉じる。

discord.py・aiohttp・asyncio の record と例外文は token・URL・ID・応答本文・traceback を含み得る。
record の中身は一切出さず、出所の系統 (固定の語) と件数だけを stderr に 1 行ずつ出す。
安全 handler 自身の失敗で、Python の既定の fallback (生の record の出力) へ落とさない。
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

# 1 系統あたり、最初の 1 件だけ出す。以後は件数に畳み、`flush` で件数だけ出す
FAMILIES = ("asyncio", "aiohttp", "discord", "other")
# 例外型名として出してよい形 (それ以外は other)
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# loop の例外を 1 行ずつ出す上限。超えた分は件数だけにする
LOOP_ERROR_LINES = 20

# asyncio が loop の exception handler に渡す message の定型句 (小文字 + `_` に正規化した形)
_LOOP_MESSAGES = frozenset({
    "exception_in_callback",
    "fatal_error_on_transport",
    "fatal_error_on_ssl_transport",
    "fatal_read_error_on_socket_transport",
    "fatal_write_error_on_socket_transport",
    "task_exception_was_never_retrieved",
    "future_exception_was_never_retrieved",
    "unhandled_exception_in_client_connected_cb",
    "exception_in_default_exception_handler",
    "accept_failed_on_a_socket",
})


def _family(name: object) -> str:
    if not isinstance(name, str):
        return "other"
    head = name.split(".", 1)[0]
    return head if head in ("asyncio", "aiohttp", "discord") else "other"


def _type_name(exc: object) -> str:
    name = type(exc).__name__
    return name if _TYPE_NAME.fullmatch(name) else "other"


def _loop_message(message: str) -> str:
    words: list[str] = []
    for word in message.split()[:8]:
        if not (word.isascii() and word.isalpha()):
            break
        words.append(word.lower())
    for count in range(len(words), 0, -1):
        if "_".join(words[:count]) in _LOOP_MESSAGES:
            return "_".join(words[:count])
    return "other"


class FixedCodeHandler(logging.Handler):
    """record を捨て、系統ごとの初回と、畳んだ件数だけを固定語で出す。"""

    def __init__(self, write: Callable[[str], None]) -> None:
        super().__init__(level=logging.WARNING)
        self._write = write
        self._counts = {family: 0 for family in FAMILIES}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            family = _family(getattr(record, "name", None))
            self._counts[family] += 1
            if self._counts[family] == 1:
                self._write(f"log_record family={family}")
        except Exception:  # noqa: BLE001 — 失敗しても生の record を出さない
            pass

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        pass

    def flush_counts(self) -> None:
        """初回の後に畳んだ件数を、系統ごとに 1 行だけ出す。"""
        try:
            for family in FAMILIES:
                repeated = self._counts[family] - 1
                if repeated > 0:
                    self._write(f"log_repeated family={family} count={repeated}")
                self._counts[family] = 0
        except Exception:  # noqa: BLE001
            pass


class LoopErrors:
    """asyncio の exception handler。例外型名と固定の定型句だけを出し、反復は件数に畳む。"""

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self.count = 0

    def __call__(self, loop: asyncio.AbstractEventLoop, context: dict) -> None:
        try:
            self.count += 1
            if self.count > LOOP_ERROR_LINES:
                return
            exc = context.get("exception")
            line = f"bot_loop_error {_type_name(exc) if exc is not None else 'none'}"
            message = context.get("message")
            if isinstance(message, str):
                line += " message=" + _loop_message(message)
            self._write(line)
        except Exception:  # noqa: BLE001
            pass

    def flush_counts(self) -> None:
        try:
            if self.count > LOOP_ERROR_LINES:
                self._write(f"bot_loop_error_repeated count={self.count - LOOP_ERROR_LINES}")
            self.count = 0
        except Exception:  # noqa: BLE001
            pass


class SafeLogging:
    """process の logging を安全 handler だけにし、`restore` で元に戻す。"""

    def __init__(self, write: Callable[[str], None]) -> None:
        self.handler = FixedCodeHandler(write)
        root = logging.getLogger()
        self._saved = (list(root.handlers), root.level, logging.lastResort,
                       logging.raiseExceptions, [])
        # 既存の handler (生の record を整形して出し得る) を外し、安全 handler だけにする
        for old in list(root.handlers):
            root.removeHandler(old)
        root.addHandler(self.handler)
        root.setLevel(logging.WARNING)
        logging.lastResort = None
        logging.raiseExceptions = False
        # 伝播先を root の安全 handler に揃える。discord は何も出さない
        for name in ("asyncio", "aiohttp"):
            logger = logging.getLogger(name)
            self._saved[4].append((logger, list(logger.handlers), logger.propagate))
            for old in list(logger.handlers):
                logger.removeHandler(old)
            logger.propagate = True
        quiet = logging.getLogger("discord")
        self._saved[4].append((quiet, list(quiet.handlers), quiet.propagate))
        quiet.addHandler(logging.NullHandler())
        quiet.propagate = False

    def flush_counts(self) -> None:
        self.handler.flush_counts()

    def restore(self) -> None:
        handlers, level, last_resort, raise_exc, loggers = self._saved
        root = logging.getLogger()
        for old in list(root.handlers):
            root.removeHandler(old)
        for old in handlers:
            root.addHandler(old)
        root.setLevel(level)
        logging.lastResort = last_resort
        logging.raiseExceptions = raise_exc
        for logger, saved, propagate in loggers:
            for old in list(logger.handlers):
                logger.removeHandler(old)
            for old in saved:
                logger.addHandler(old)
            logger.propagate = propagate

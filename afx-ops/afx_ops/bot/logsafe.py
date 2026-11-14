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


# 型名を出してよい例外型。module の import 時に固定した型の集合 (同じ名前を後から公開した動的な型や
# 利用側の型は、名前が識別子の形でも出さない)。比較は型そのものの同一性で行う
def _allowed_types() -> frozenset[type]:
    import builtins
    import concurrent.futures
    import json
    import socket
    import ssl

    import aiohttp
    import discord

    found: set[type] = {value for value in vars(builtins).values()
                        if isinstance(value, type) and issubclass(value, BaseException)}
    named = {
        asyncio: ("CancelledError", "TimeoutError", "InvalidStateError", "IncompleteReadError",
                  "LimitOverrunError", "QueueEmpty", "QueueFull"),
        concurrent.futures: ("CancelledError", "TimeoutError", "BrokenExecutor"),
        json: ("JSONDecodeError",),
        socket: ("gaierror", "herror", "timeout"),
        ssl: ("SSLError", "SSLCertVerificationError", "SSLEOFError", "SSLZeroReturnError",
              "SSLWantReadError", "SSLWantWriteError", "SSLSyscallError"),
        aiohttp: ("ClientError", "ClientConnectionError", "ClientConnectorError",
                  "ClientOSError", "ClientPayloadError", "ClientResponseError",
                  "ServerDisconnectedError", "ServerTimeoutError", "ClientConnectorSSLError",
                  "ClientConnectorCertificateError", "WSServerHandshakeError"),
        discord: ("DiscordException", "ClientException", "HTTPException", "Forbidden",
                  "NotFound", "DiscordServerError", "LoginFailure", "ConnectionClosed",
                  "GatewayNotFound", "RateLimited", "InvalidData"),
    }
    for module, names in named.items():
        for name in names:
            value = getattr(module, name, None)
            if isinstance(value, type) and issubclass(value, BaseException):
                found.add(value)
    return frozenset(found)


_ALLOWED_TYPES = _allowed_types()


def _type_name(exc: object) -> str:
    kind = type(exc)
    name = getattr(kind, "__name__", "")
    if (kind not in _ALLOWED_TYPES or not isinstance(name, str)
            or _TYPE_NAME.fullmatch(name) is None):
        return "other"
    return name


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


def _family_loggers(root_name: str) -> list[logging.Logger]:
    """系統の logger 本体と、既に作られている子孫 (直接 handler を持ち得る)。"""
    found = [logging.getLogger(root_name)]
    prefix = root_name + "."
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith(prefix) and isinstance(logger, logging.Logger):
            found.append(logger)
    return found


class SafeLogging:
    """process の logging を安全 handler だけにし、`restore` で元に戻す。

    構築の途中で失敗・中断しても、それまでに変えた分を戻してから例外を出す。
    """

    def __init__(self, write: Callable[[str], None]) -> None:
        self.handler = FixedCodeHandler(write)
        root = logging.getLogger()
        self._saved = (list(root.handlers), root.level, logging.lastResort,
                       logging.raiseExceptions, [])
        try:
            self._install(root)
        except BaseException:
            self.restore()
            raise

    def _install(self, root: logging.Logger) -> None:
        # 既存の handler (生の record を整形して出し得る) を外し、安全 handler だけにする
        for old in list(root.handlers):
            root.removeHandler(old)
        root.addHandler(self.handler)
        root.setLevel(logging.WARNING)
        logging.lastResort = None
        logging.raiseExceptions = False
        # 伝播先を root の安全 handler に揃える。子孫が直接持つ handler も外す。discord は何も出さない
        for name in ("asyncio", "aiohttp"):
            for logger in _family_loggers(name):
                self._saved[4].append((logger, list(logger.handlers), logger.propagate))
                for old in list(logger.handlers):
                    logger.removeHandler(old)
                logger.propagate = True
        for logger in _family_loggers("discord"):
            self._saved[4].append((logger, list(logger.handlers), logger.propagate))
            for old in list(logger.handlers):
                logger.removeHandler(old)
        quiet = logging.getLogger("discord")
        quiet.addHandler(logging.NullHandler())
        quiet.propagate = False

    def set_write(self, write: Callable[[str], None]) -> None:
        self.handler._write = write

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
        loggers.clear()


# 起動の最初 (import・設定の読込みより前) に入れた安全 logging。`run` が引き取る
_EARLY: SafeLogging | None = None


def install_early(write: Callable[[str], None]) -> SafeLogging:
    global _EARLY
    if _EARLY is not None:
        _EARLY.restore()
    _EARLY = SafeLogging(write)
    return _EARLY


def take_early() -> SafeLogging | None:
    global _EARLY
    early, _EARLY = _EARLY, None
    return early

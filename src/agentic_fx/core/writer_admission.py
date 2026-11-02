"""scheduler の DB 書込み要求フラグと、worker の書込み開始の全順序化。

scheduler は DB に書く可能性がある tick の開始前に `request()` でフラグを立て、
その tick で開いた write transaction がすべて終わってから `release()` で下ろす。
worker は新しい transaction を始める前に `admit(begin)` を通す。`admit` は mutex の中で
フラグの確認と進行中の登録だけを行い、`begin` (BEGIN IMMEDIATE) は mutex の外で呼ぶ。
フラグが立っている間は `begin` を呼ばず False を返す。確認と BEGIN の間に割り込まれた
BEGIN は、busy_timeout 0 の接続で即 BUSY になる。

フラグは scheduler thread の入れ子 (tick の中の tick など) を数える。最も外側の
`release()` で下り、立っていた時間をその時点で記録する。tick の印と次 tick の予定は
負荷抑制の観測値で、書込みの排他の正しさには使わない。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass(frozen=True, slots=True)
class WriterAdmissionStats:
    requested: bool
    requests: int
    last_hold_sec: float | None
    max_hold_sec: float | None
    total_hold_sec: float
    yielded: int
    admitted: int
    beginning: int
    tick_active: bool
    next_tick_at: float | None


class WriterAdmission:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._mutex = threading.Lock()
        self._depth = 0
        self._since: float | None = None
        self._requests = 0
        self._last_hold: float | None = None
        self._max_hold: float | None = None
        self._total_hold = 0.0
        self._yielded = 0
        self._admitted = 0
        self._beginning = 0
        self._tick_active = False
        self._next_tick_at: float | None = None

    # --- scheduler 側 ------------------------------------------------------

    def request(self) -> None:
        """書込み要求フラグを立てる。書込みを始める前に必ず呼ぶ。"""
        with self._mutex:
            self._depth += 1
            if self._depth == 1:
                self._since = self._monotonic()
                self._requests += 1

    def release(self) -> None:
        """要求を 1 つ下ろす。最後の 1 つで、フラグが立っていた時間を記録する。"""
        with self._mutex:
            if self._depth == 0:
                raise RuntimeError("writer admission released without a request")
            self._depth -= 1
            if self._depth == 0:
                held = self._monotonic() - (self._since or 0.0)
                self._since = None
                self._last_hold = held
                self._max_hold = held if self._max_hold is None else max(self._max_hold, held)
                self._total_hold += held

    @contextmanager
    def scheduler_write(self) -> Iterator[None]:
        self.request()
        try:
            yield
        finally:
            self.release()

    # --- worker 側 ---------------------------------------------------------

    def requested(self) -> bool:
        with self._mutex:
            return self._depth > 0

    def admit(self, begin: Callable[[], None]) -> bool:
        """フラグが立っていなければ `begin()` を呼んで True を返す。

        mutex の中で行うのはフラグの確認と進行中の登録だけで、`begin` は mutex の外で
        呼ぶ (BEGIN の待ちで scheduler の `request()` を止めない)。確認と BEGIN の間に
        scheduler が登録すれば、worker の BEGIN は busy_timeout 0 の接続で即 BUSY に
        なり、呼出し側が次の機会に取り直す。フラグが立っていれば `begin` を呼ばず False。
        `begin` が例外を投げたら進行中の登録を戻して再送出する。
        """
        with self._mutex:
            if self._depth > 0:
                self._yielded += 1
                return False
            self._beginning += 1
        try:
            begin()
        except BaseException:
            with self._mutex:
                self._beginning -= 1
            raise
        with self._mutex:
            self._beginning -= 1
            self._admitted += 1
        return True

    # --- 観測 (正しさには使わない) -----------------------------------------

    def tick_started(self) -> None:
        with self._mutex:
            self._tick_active = True

    def tick_finished(self) -> None:
        with self._mutex:
            self._tick_active = False

    def note_next_tick(self, at: float) -> None:
        """次の tick を始める予定の monotonic 時刻。"""
        with self._mutex:
            self._next_tick_at = at

    def stats(self) -> WriterAdmissionStats:
        with self._mutex:
            return WriterAdmissionStats(
                requested=self._depth > 0, requests=self._requests,
                last_hold_sec=self._last_hold, max_hold_sec=self._max_hold,
                total_hold_sec=self._total_hold, yielded=self._yielded,
                admitted=self._admitted, beginning=self._beginning,
                tick_active=self._tick_active,
                next_tick_at=self._next_tick_at)

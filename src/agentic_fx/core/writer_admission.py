"""scheduler の DB 書込み要求フラグと、worker の書込みの全順序化。

scheduler は DB に書く可能性がある tick の開始前に `request()` でフラグを立て、
その tick で開いた write transaction がすべて終わってから `release()` で下ろす。
worker は新しい transaction を始める前に `admit(begin)` を通す。`admit` は mutex の中で
フラグの確認と進行中の登録だけを行い、`begin` (BEGIN IMMEDIATE) は mutex の外で呼ぶ。
フラグが立っている間は `begin` を呼ばず False を返す。確認と BEGIN の間に割り込まれた
BEGIN は、busy_timeout 0 の接続で即 BUSY になる。

worker の admission は transaction の lease で、BEGIN の前から commit / rollback の後まで
続く。`request()` は新規開始を閉じたうえで、すでに始まっている lease の終了を上限付きで
待つ (hold.down 後に始まった worker の transaction の後ろに scheduler が並ばない)。

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


#: scheduler の `request()` が、すでに始まっている worker の transaction の終了を待つ上限
DRAIN_WAIT_SEC = 0.2


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
    wait_overruns: int
    last_wait_sec: float | None
    max_wait_sec: float | None
    tick_active: bool
    next_tick_at: float | None


class WriterAdmission:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 drain_wait_sec: float = DRAIN_WAIT_SEC) -> None:
        self._monotonic = monotonic
        self._drain_wait_sec = drain_wait_sec
        self._mutex = threading.Lock()
        self._cond = threading.Condition(self._mutex)
        self._wait_overruns = 0
        self._last_wait: float | None = None
        self._max_wait: float | None = None
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
        """書込み要求フラグを立てる。書込みを始める前に必ず呼ぶ。

        立てた時点で worker の新規開始は閉じる。最も外側の要求は、すでに始まっている
        worker の transaction (lease) が終わるのを `drain_wait_sec` を上限に待つ。
        上限を超えたら待たずに進み、超過を観測値に数える。
        """
        with self._cond:
            self._depth += 1
            if self._depth != 1:
                return
            self._since = self._monotonic()
            self._requests += 1
            started = time.monotonic()
            deadline = started + self._drain_wait_sec
            while self._beginning > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._wait_overruns += 1
                    break
                self._cond.wait(remaining)
            waited = time.monotonic() - started
            self._last_wait = waited
            self._max_wait = waited if self._max_wait is None else max(self._max_wait, waited)

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
        """フラグが立っていなければ `begin()` を呼んで True を返す。True の間は
        transaction の lease を保持する。commit / rollback の後に必ず `end()` で返す
        (`lease()` を使えば返し忘れない)。

        mutex の中で行うのはフラグの確認と進行中の登録だけで、`begin` は mutex の外で
        呼ぶ (BEGIN の待ちで scheduler の `request()` を止めない)。確認と BEGIN の間に
        scheduler が登録すれば、worker の BEGIN は busy_timeout 0 の接続で即 BUSY に
        なり、呼出し側が次の機会に取り直す。フラグが立っていれば `begin` を呼ばず False。
        `begin` が例外を投げたら lease を戻して再送出する。
        """
        with self._mutex:
            if self._depth > 0:
                self._yielded += 1
                return False
            self._beginning += 1
        try:
            begin()
        except BaseException:
            self.end()
            raise
        with self._mutex:
            self._admitted += 1
        return True

    def end(self) -> None:
        """`admit` が True を返した transaction の lease を返す。"""
        with self._cond:
            if self._beginning == 0:
                raise RuntimeError("writer admission ended without a lease")
            self._beginning -= 1
            if self._beginning == 0:
                self._cond.notify_all()

    @contextmanager
    def lease(self, begin: Callable[[], None]) -> Iterator[bool]:
        """`admit` と `end` の対。with の中で transaction を終わらせる (commit /
        rollback)。通らなかった (False) ときは何も保持しない。"""
        if not self.admit(begin):
            yield False
            return
        try:
            yield True
        finally:
            self.end()

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
                wait_overruns=self._wait_overruns,
                last_wait_sec=self._last_wait, max_wait_sec=self._max_wait,
                tick_active=self._tick_active,
                next_tick_at=self._next_tick_at)

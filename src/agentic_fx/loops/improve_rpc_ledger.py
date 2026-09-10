"""ImproveRpcLedger — Mission ごとの RPC 台帳 (設計書 §3.4、§8.1-15)。

lock 付き状態機械 OPEN -> FROZEN -> PERSISTED | DISCARDED。dispatcher
スレッドと slot スレッドの橋渡し役 (§3.1「台帳だけが両スレッドの橋」) — 接続を
スレッド間で共有しない代わりに、この薄いオブジェクトだけを共有する。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Literal


class ImproveRpcLedger:
    def __init__(self, *, rpc_timeout_sec_by_kind: dict[str, float]) -> None:
        self._rpc_timeout_sec_by_kind = dict(rpc_timeout_sec_by_kind)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._state: Literal[
            "OPEN", "FROZEN", "PERSIST_FAILED", "PERSISTED", "DISCARDED"
        ] = "OPEN"
        self._entries: list[dict] = []
        self._generation = 0
        self._next_token = 0
        self._reservations = 0

    def begin_accept(self) -> tuple[int, int] | None:
        with self._condition:
            if self._state != "OPEN":
                return None
            reservation = (self._generation, self._next_token)
            self._next_token += 1
            self._reservations += 1
            return reservation

    def end_accept(self, reservation: tuple[int, int] | None) -> None:
        if reservation is None:
            return
        generation, _token = reservation
        with self._condition:
            if generation != self._generation:
                return
            if self._reservations <= 0:
                return
            self._reservations -= 1
            self._condition.notify_all()

    def record(self, *, opaque_ref: str, kind: str, params: dict,
               result_summary: dict, trial_count: int) -> None:
        with self._lock:
            if self._state != "OPEN":
                return  # FROZEN 後は無視 (§3.4「遅延結果は捨てる」)
            self._entries.append({
                "opaque_ref": opaque_ref, "kind": kind, "params": params,
                "result_summary": result_summary, "trial_count": trial_count})

    def freeze(self, *, drain_timeout_sec: float = 0.0,
               on_timeout: Callable[[int], None] | None = None) -> None:
        dropped = 0
        with self._condition:
            if self._state != "OPEN":
                raise RuntimeError(f"cannot freeze from state {self._state!r}")
            deadline = time.monotonic() + max(drain_timeout_sec, 0.0)
            while self._reservations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    dropped = self._reservations
                    self._reservations = 0
                    self._generation += 1
                    break
                self._condition.wait(remaining)
            self._state = "FROZEN"
        if dropped and on_timeout is not None:
            on_timeout(dropped)

    def freeze_if_open(self, **kwargs) -> None:
        with self._lock:
            is_open = self._state == "OPEN"
        if is_open:
            try:
                self.freeze(**kwargs)
            except RuntimeError:
                pass

    def entries(self) -> list[dict]:
        with self._lock:
            if self._state == "OPEN":
                raise RuntimeError("entries() is only valid after freeze()")
            return list(self._entries)

    def state(self) -> str:
        with self._lock:
            return self._state

    def mark_persisted(self) -> None:
        with self._lock:
            if self._state not in ("FROZEN", "PERSIST_FAILED"):
                raise RuntimeError(
                    f"cannot mark_persisted from state {self._state!r}")
            self._state = "PERSISTED"

    def mark_discarded(self) -> None:
        with self._lock:
            if self._state not in ("FROZEN", "PERSIST_FAILED"):
                raise RuntimeError(
                    f"cannot mark_discarded from state {self._state!r}")
            self._state = "DISCARDED"

    def mark_persist_failed(self) -> None:
        with self._lock:
            if self._state != "FROZEN":
                raise RuntimeError(
                    f"cannot mark_persist_failed from state {self._state!r}")
            self._state = "PERSIST_FAILED"

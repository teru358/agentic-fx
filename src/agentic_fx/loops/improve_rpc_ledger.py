"""ImproveRpcLedger — Mission ごとの RPC 台帳 (設計書 §3.4、§8.1-15)。

lock 付き状態機械 OPEN -> FROZEN -> PERSISTED | DISCARDED。dispatcher
スレッドと slot スレッドの橋渡し役 (§3.1「台帳だけが両スレッドの橋」) — 接続を
スレッド間で共有しない代わりに、この薄いオブジェクトだけを共有する。
"""
from __future__ import annotations

import threading
from typing import Literal


class ImproveRpcLedger:
    def __init__(self, *, rpc_timeout_sec_by_kind: dict[str, float]) -> None:
        self._rpc_timeout_sec_by_kind = dict(rpc_timeout_sec_by_kind)
        self._lock = threading.Lock()
        self._state: Literal["OPEN", "FROZEN", "PERSISTED", "DISCARDED"] = "OPEN"
        self._entries: list[dict] = []

    def record(self, *, opaque_ref: str, kind: str, params: dict,
               result_summary: dict, trial_count: int) -> None:
        with self._lock:
            if self._state != "OPEN":
                return  # FROZEN 後は無視 (§3.4「遅延結果は捨てる」)
            self._entries.append({
                "opaque_ref": opaque_ref, "kind": kind, "params": params,
                "result_summary": result_summary, "trial_count": trial_count})

    def freeze(self) -> None:
        with self._lock:
            if self._state != "OPEN":
                raise RuntimeError(f"cannot freeze from state {self._state!r}")
            self._state = "FROZEN"

    def entries(self) -> list[dict]:
        with self._lock:
            if self._state == "OPEN":
                raise RuntimeError("entries() is only valid after freeze()")
            return list(self._entries)

    def mark_persisted(self) -> None:
        with self._lock:
            if self._state != "FROZEN":
                raise RuntimeError(
                    f"cannot mark_persisted from state {self._state!r}")
            self._state = "PERSISTED"

    def mark_discarded(self) -> None:
        with self._lock:
            if self._state != "FROZEN":
                raise RuntimeError(
                    f"cannot mark_discarded from state {self._state!r}")
            self._state = "DISCARDED"

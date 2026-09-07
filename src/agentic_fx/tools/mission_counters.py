"""Mission-local counters shared by improve tools (設計 v4 Tier B/C).

API は `reserve_*` (上限チェック + 加算を同一 lock 区間で) と `record_*_result`
(実行結果の記録) の 2 段のみ。codex 2 周目 (2026-09-07) の裁定で budget=None
専用の旧 API (`record_self_test` / `record_backtest` / `record_write`) は削除 —
builder は counters と budget を **両方** 受け取るか **両方** 省く。"""
from __future__ import annotations

from collections import defaultdict
from threading import Lock


class MissionToolCounters:
    def __init__(self) -> None:
        self._lock = Lock()
        self.total_calls = 0
        self.writes = 0
        self.self_test_runs = 0
        self.self_tests_before_backtest = 0
        self.successful_backtests = defaultdict(int)
        self.backtest_calls = defaultdict(int)
        self.last_signature: dict[str, tuple] = {}
        self.consecutive_same = defaultdict(int)
        self._self_tests_by_name = defaultdict(int)

    def record_call(self) -> None:
        with self._lock:
            self.total_calls += 1

    def reserve_write(self, limit: int) -> bool:
        with self._lock:
            if self.writes >= limit:
                return False
            self.writes += 1
            return True

    def reserve_self_test(
            self, name: str, *, max_runs: int,
            max_before_backtest: int, is_strategy: bool) -> str | None:
        """Atomically admit and count one self-test, returning rejection kind."""
        with self._lock:
            if self.self_test_runs >= max_runs:
                return "max_self_test_runs"
            before_backtest = is_strategy and self.successful_backtests[name] == 0
            if before_backtest and (
                    self._self_tests_by_name[name] >= 1
                    or self.self_tests_before_backtest >= max_before_backtest):
                return "run_backtest_required_first"
            self.self_test_runs += 1
            self._self_tests_by_name[name] += 1
            if before_backtest:
                self.self_tests_before_backtest += 1
            return None

    def record_self_test_result(self, name: str, signature: tuple) -> int:
        with self._lock:
            if self.last_signature.get(name) == signature:
                self.consecutive_same[name] += 1
            else:
                self.last_signature[name] = signature
                self.consecutive_same[name] = 1
            return self.consecutive_same[name]

    def reserve_backtest(self, name: str, limit: int) -> bool:
        with self._lock:
            if self.backtest_calls[name] >= limit:
                return False
            self.backtest_calls[name] += 1
            return True

    def record_backtest_result(self, name: str, ok: bool) -> None:
        if not ok:
            return
        with self._lock:
            self.successful_backtests[name] += 1


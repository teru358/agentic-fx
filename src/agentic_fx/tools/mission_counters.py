"""Mission-local counters shared by improve tools (設計 v4 Tier B/C).

API は `reserve_*` (上限チェック + 加算を同一 lock 区間で) と `record_*_result`
(実行結果の記録) の 2 段のみ。codex 2 周目 (2026-09-07) の裁定で budget=None
専用の旧 API (`record_self_test` / `record_backtest` / `record_write`) は削除 —
builder は counters と budget を **両方** 受け取るか **両方** 省く。"""
from __future__ import annotations

from collections import defaultdict
from threading import Event, Lock
from typing import Any, Literal


class MissionToolCounters:
    def __init__(self, budget: Any | None = None) -> None:
        self._lock = Lock()
        self._budget = budget
        self.abort_event = Event()
        self.abort_pending = False
        self.abort_trigger: str | None = None
        self.terminal_refusal_streak = 0
        self.recoverable_refusal_streak = defaultdict(int)
        self.refusals = 0
        self.errors = 0   # tool の例外 / 引数不正 / error 応答 (run8 是正)
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
            self._check_threshold(self.total_calls,
                                  getattr(self._budget, "max_tool_calls", None),
                                  "max_tool_calls")

    def _check_threshold(self, value: int, limit: int | None, trigger: str) -> None:
        """3 本の閾値 (総呼び出し / terminal / recoverable) を 1 箇所で判定する
        (/code-review 2 周目 #7)。budget 無し (limit None) は判定しない。
        呼び出し側が lock を保持している前提。"""
        if limit is not None and value >= limit:
            self._mark_abort(trigger)

    def _mark_abort(self, trigger: str) -> None:
        if not self.abort_pending:
            self.abort_pending = True
            self.abort_trigger = trigger

    def fire_if_pending(self) -> None:
        with self._lock:
            if self.abort_pending:
                self.abort_event.set()

    def record_terminal_refusal(self) -> None:
        with self._lock:
            self.refusals += 1
            self.terminal_refusal_streak += 1
            self._check_threshold(self.terminal_refusal_streak,
                                  getattr(self._budget, "max_refusal_streak", None),
                                  "terminal_refusals")

    def record_recoverable_refusal(self, name: str, reason: str) -> None:
        with self._lock:
            self.refusals += 1
            key = (name, reason)
            self.recoverable_refusal_streak[key] += 1
            self._check_threshold(self.recoverable_refusal_streak[key],
                                  getattr(self._budget, "max_refusal_streak", None),
                                  f"recoverable_refusals:{name}")

    def record_tool_result(self, name: str, ok: bool, error: str | None = None) -> None:
        """registry の `on_result` フック (run8 是正 [tool-exception-bypasses-
        refusal-streak])。streak のキーは (tool 名, 失敗種別): `exception` /
        `invalid_args` はそれぞれ集約、tool が返した error 文字列は先頭 60 字で
        区別する (codex Important 1: run_backtest の no_history / loader_rejected /
        backtest_failed のような入力依存の業務エラーを 1 本に合算して「壊れた
        tool」扱いしない)。同じ (tool, 種別) が `max_refusal_streak` 回続いたら
        abort (`tool_errors:<name>`)。同じ tool の成功でその tool の全種別を 0 に
        戻す。別 tool の成功では戻らない。"""
        with self._lock:
            if ok:
                for key in [k for k in self.recoverable_refusal_streak
                            if k[0] == name and k[1].startswith("tool_error:")]:
                    self.recoverable_refusal_streak[key] = 0
                return
            self.errors += 1
            key = (name, "tool_error:" + (error or "exception")[:60])
            self.recoverable_refusal_streak[key] += 1
            self._check_threshold(self.recoverable_refusal_streak[key],
                                  getattr(self._budget, "max_refusal_streak", None),
                                  f"tool_errors:{name}")

    def record_progress(
            self, name: str,
            kind: Literal["backtest_ok", "self_test_ran"]) -> None:
        with self._lock:
            self.terminal_refusal_streak = 0
            if kind == "backtest_ok":
                self.recoverable_refusal_streak[(
                    name, "run_backtest_required_first")] = 0

    def summary(self) -> str:
        with self._lock:
            return (f"calls={self.total_calls} refused={self.refusals} "
                    f"errors={self.errors} self_test={self.self_test_runs} "
                    f"backtest={sum(self.backtest_calls.values())}")

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

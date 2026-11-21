"""取引判断 mission の手数制御 (local runner 専用)。

最後の 2 ターンはツールを止めて判断の JSON を出させ、同じ呼出しの再実行と
検索の回数を抑える。状態は `LocalRunner.run` の 1 回ごとに作って捨てる
(mission をまたいで持ち越さない)。policy が None の mission では使わない。
"""
from __future__ import annotations

import json
from typing import Any

POLICY_SIGNAL = "signal"
POLICY_CRON = "cron"
POLICIES = (POLICY_SIGNAL, POLICY_CRON)

#: policy 有効時に必要な最小の max_turns (調査 1 + 最終 2)。
MIN_TURNS = 3
FINAL_STAGE_TURNS = 2
SEARCH_TOOL = "search_news"
#: 同一呼出しの抑止と再試行の上限を適用するツール。mission の間は結果が変わらない
#: (建玉は発注時に risk gate が確かめ直す)。価格・足など値が変わるツールは対象外。
DEDUPE_TOOLS = ("search_news", "get_positions")
SEARCH_BUDGET = 5
#: 同じ引数の取得失敗は 1 回だけ再試行を許す (失敗 2 回で打ち止め)。
MAX_FAILURES_PER_KEY = 2

REASON_SEARCH_UNAVAILABLE = "search_unavailable"
REASON_FINAL_ANSWER_MISSING = "final_answer_missing"

FINAL_ONLY_TEXT = "最終段階のため実行しない。判断の JSON だけを出す"
SWITCH_TEXT = "ツールは使えない。JSON だけを出す。"
_SEARCH_BUDGET_TEXT = "検索の予算に達した。これまでの材料で判断して JSON を出す"
_DUPLICATE_TEXT = "既に得た結果。判断して JSON を出す"
_DUPLICATE_FAILED_TEXT = "取得に失敗済み。これ以上は実行しない。手元の材料で判断して JSON を出す"

_TEXT_CALL_MARKERS = ("<tool_call>", "<function=")


def _error(code: str, message: str) -> str:
    return json.dumps({"error": code, "message": message}, ensure_ascii=False)


FINAL_ONLY_RESULT = _error("final_answer_only", FINAL_ONLY_TEXT)
DUPLICATE_RESULT = _error("duplicate_call", _DUPLICATE_TEXT)
DUPLICATE_FAILED_RESULT = _error("duplicate_call", _DUPLICATE_FAILED_TEXT)
SEARCH_BUDGET_RESULT = _error("search_budget_exhausted", _SEARCH_BUDGET_TEXT)


def turn_notice(policy: str, max_turns: int) -> str:
    """mission.prompt の末尾に足す、上限と最終段階の固定文。"""
    text = (f"\n\n総上限は {max_turns} ターン。調査は {max_turns - FINAL_STAGE_TURNS} "
            f"ターンまで。最後の 2 ターンはツールを使わず JSON を出す。")
    if policy == POLICY_SIGNAL:
        text += f"検索の実行予算は {SEARCH_BUDGET} 回。"
    return text


def switch_text(schema: dict) -> str:
    """最終段階に入るときの切替文 (出力 schema 付き)。"""
    return SWITCH_TEXT + "\n出力 schema:\n" + json.dumps(schema, ensure_ascii=False)


def is_text_tool_call(content: str) -> bool:
    """content が文字列によるツール呼出し (template が指示する包み) を含むか。"""
    return any(marker in content for marker in _TEXT_CALL_MARKERS)


def canonical_key(name: str, arguments: Any) -> str:
    """同一呼出しの比較キー。object のキー順と書式空白は無視し、配列順・
    文字列の中身・値の型は保つ。"""
    return name + "\0" + json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str)


class TurnGuard:
    """1 回の mission 実行の手数・呼出し・検索の状態。"""

    def __init__(self, policy: str, max_turns: int) -> None:
        self.policy = policy
        self.max_turns = max_turns
        self.final_stage = False
        self.requests = 0
        self.final_entered = False
        self.decided_in_final = False
        self.budget_replied = False
        self.decided_after_budget = False
        self.search_attempted = False
        self.search_executed = 0
        self.search_succeeded = 0
        self.search_invalid = 0
        self.search_exec_error = 0
        self.search_duplicate = 0
        self.search_over_budget = 0
        self.duplicate_suppressed = 0
        self.final_stage_suppressed = 0
        self._succeeded: set[str] = set()
        self._failures: dict[str, int] = {}

    # ---- runner から ----------------------------------------------------

    def begin_request(self, turn: int) -> None:
        """1 始まりの turn の要求を送る直前。"""
        self.requests += 1
        self.final_stage = turn >= self.max_turns - FINAL_STAGE_TURNS + 1
        if self.final_stage:
            self.final_entered = True

    def note_call(self, name: object) -> None:
        """ツール名を識別した時点の試行記録 (引数の検証や抑止より前)。
        最終段階の呼出しは実行を試みないので数えない。"""
        if name == SEARCH_TOOL and not self.final_stage:
            self.search_attempted = True

    def note_invalid(self, name: object) -> None:
        """引数・許可の検証エラーで実行に進まなかった呼出し。"""
        if name == SEARCH_TOOL:
            self.search_invalid += 1

    def note_answer(self) -> None:
        self.decided_in_final = self.final_stage
        self.decided_after_budget = self.budget_replied

    @property
    def search_unavailable(self) -> bool:
        """検索を試みたが 1 回も成功していない。"""
        return self.search_attempted and self.search_succeeded == 0

    # ---- registry から (検証後・関数呼出し前 / 実行後) ---------------------

    def decide(self, name: str, arguments: Any) -> str | None:
        """実行しない場合の固定結果を返す。None なら実行する。"""
        if self.final_stage:
            self.final_stage_suppressed += 1
            return FINAL_ONLY_RESULT
        if name not in DEDUPE_TOOLS:
            return None
        key = canonical_key(name, arguments)
        if key in self._succeeded:
            return self._duplicate(name, DUPLICATE_RESULT)
        if self._failures.get(key, 0) >= MAX_FAILURES_PER_KEY:
            return self._duplicate(name, DUPLICATE_FAILED_RESULT)
        if name == SEARCH_TOOL and self.policy == POLICY_SIGNAL:
            if self.search_executed >= SEARCH_BUDGET:
                self.search_over_budget += 1
                self.budget_replied = True
                return SEARCH_BUDGET_RESULT
        if name == SEARCH_TOOL:
            self.search_executed += 1
        return None

    def record(self, name: str, arguments: Any, ok: bool) -> None:
        if name in DEDUPE_TOOLS:
            key = canonical_key(name, arguments)
            if ok:
                self._succeeded.add(key)
            else:
                self._failures[key] = self._failures.get(key, 0) + 1
        if name == SEARCH_TOOL:
            if ok:
                self.search_succeeded += 1
            else:
                self.search_exec_error += 1

    def _duplicate(self, name: str, result: str) -> str:
        self.duplicate_suppressed += 1
        if name == SEARCH_TOOL:
            self.search_duplicate += 1
        return result

    # ---- 終端要約 -------------------------------------------------------

    def summary(self, status: str, reason: str | None) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "final_stage": self.final_entered,
            "decided_in_final": self.decided_in_final,
            "decided_after_budget": self.decided_after_budget,
            "search_attempted": self.search_attempted,
            "search_executed": self.search_executed,
            "search_succeeded": self.search_succeeded,
            "search_invalid": self.search_invalid,
            "search_exec_error": self.search_exec_error,
            "search_duplicate": self.search_duplicate,
            "search_over_budget": self.search_over_budget,
            "duplicate_suppressed": self.duplicate_suppressed,
            "final_stage_suppressed": self.final_stage_suppressed,
            "terminal": status,
            "reason": (" ".join(reason.split())[:_MAX_REASON] if reason else None),
        }


_BOOL_FIELDS = ("final_stage", "decided_in_final", "decided_after_budget",
                "search_attempted")
_INT_FIELDS = ("requests", "search_executed", "search_succeeded", "search_invalid",
               "search_exec_error", "search_duplicate", "search_over_budget",
               "duplicate_suppressed", "final_stage_suppressed")
_STATUSES = ("completed", "failed", "timeout", "max_turns")
_MAX_REASON = 120
MISSING_SUMMARY: dict[str, Any] = {"missing": True}


def parse_turn_summary(value: object) -> dict[str, Any] | None:
    """worker から届いた要約を検証する。形が合わなければ None (捨てる)。"""
    if not isinstance(value, dict):
        return None
    expected = {*_BOOL_FIELDS, *_INT_FIELDS, "terminal", "reason"}
    if set(value) != expected:
        return None
    for key in _BOOL_FIELDS:
        if not isinstance(value[key], bool):
            return None
    for key in _INT_FIELDS:
        v = value[key]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return None
    if value["terminal"] not in _STATUSES:
        return None
    reason = value["reason"]
    if reason is not None and (not isinstance(reason, str)
                               or len(reason) > _MAX_REASON):
        return None
    return {key: value[key] for key in sorted(expected)}


def format_turn_summary(summary: dict[str, Any]) -> str:
    """activity 一行の要約文 (本文・引数を含まない)。"""
    if summary.get("missing"):
        return "missing=1"
    parts = []
    for key in (*_INT_FIELDS, *_BOOL_FIELDS):
        value = summary[key]
        parts.append(f"{key}={int(value) if isinstance(value, bool) else value}")
    parts.append(f"terminal={summary['terminal']}")
    parts.append(f"reason={summary['reason'] or '-'}")
    return " ".join(parts)

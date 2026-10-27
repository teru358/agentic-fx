"""改善 mission の繰り返し実行の純粋な部分 (client と daemon が共有する)。

期間・回数・一時指示の文法、停止判定の純関数、一時指示の保持と消去、
mission の結果の正規形を持つ。I/O も時計も持たない (時刻は引数で受ける)。
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass

MIN_DURATION_SEC = 60
MAX_DURATION_SEC = 86400
MAX_RUNS = 24
COMMENT_MAX_CHARS = 1000
CONSECUTIVE_FAILURE_LIMIT = 2
BACKOFF_SEC = 300
WAIT_RECHECK_SEC = 30

STOP_DEADLINE = "deadline"
STOP_MAX_RUNS = "max_runs"
STOP_STOPPED = "stopped"
STOP_CONSECUTIVE_FAILURES = "consecutive_failures"
STOP_RESOURCE_EXHAUSTED = "resource_exhausted"
STOP_AUTOPILOT = "autopilot"
STOP_SHUTDOWN = "shutdown"
STOP_REASONS = (
    STOP_DEADLINE, STOP_MAX_RUNS, STOP_STOPPED, STOP_CONSECUTIVE_FAILURES,
    STOP_RESOURCE_EXHAUSTED, STOP_AUTOPILOT, STOP_SHUTDOWN)
StopReason = str

FAILURE_RESOURCE_EXHAUSTED = "resource_exhausted"
FAILURE_RUNNER_FAULT = "runner_fault"
FAILURE_CLASSES = frozenset({FAILURE_RESOURCE_EXHAUSTED, FAILURE_RUNNER_FAULT})

#: executor 固有の status。DB の `missions.status` には現れない。
STATUS_SHUTDOWN = "shutdown"

_DURATION_RE = re.compile(r"(?:([0-9]+)h)?(?:([0-9]+)m)?\Z")
_ALLOWED_SPACES = frozenset(" \t\r\n")


def parse_duration(text: str) -> int:
    """`2h` `45m` `1h30m` を秒にする。合計 1 分以上 24 時間以下。"""
    if not isinstance(text, str):
        raise ValueError("invalid duration")
    m = _DURATION_RE.match(text)
    if m is None or not text or (m.group(1) is None and m.group(2) is None):
        raise ValueError("invalid duration")
    total = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    return validate_duration_sec(total)


def validate_duration_sec(seconds: object) -> int:
    """期間 (秒) の型と範囲の検査。client の解釈と daemon の受理が同じ規則を使う。"""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("invalid duration")
    if seconds < MIN_DURATION_SEC or seconds > MAX_DURATION_SEC:
        raise ValueError("duration out of range")
    return seconds


def validate_max_runs(n: object) -> int:
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError("invalid max runs")
    if n < 1 or n > MAX_RUNS:
        raise ValueError("max runs out of range")
    return n


def normalize_comment(text: str) -> str:
    """一時指示の文法。許す空白は space / TAB / CR / LF だけ。"""
    if not isinstance(text, str):
        raise ValueError("invalid comment")
    out: list[str] = []
    pending_space = False
    for ch in text:
        if ch in _ALLOWED_SPACES:
            pending_space = True
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Zs", "Zl", "Zp") or ch == "\x00":
            raise ValueError("invalid character in comment")
        if pending_space and out:
            out.append(" ")
        pending_space = False
        out.append(ch)
    folded = "".join(out)
    if not folded:
        raise ValueError("empty comment")
    if len(folded) > COMMENT_MAX_CHARS:
        raise ValueError("comment too long")
    return folded


class CommentHolder:
    """一時指示を保持する。`clear()` の後は `None`。"""

    def __init__(self, comment: str | None = None) -> None:
        self._lock = threading.Lock()
        self._comment = comment

    def get(self) -> str | None:
        with self._lock:
            return self._comment

    def clear(self) -> None:
        with self._lock:
            self._comment = None


@dataclass(frozen=True)
class MissionOutcome:
    """1 mission の正規形。`status` は補償後の `missions.status`、または `shutdown`。"""

    status: str
    failure_class: str | None
    mission_id: int | None


def decide_next(*, runs: int, max_runs: int, mono_now: float,
                deadline_mono: float, consecutive_failures: int,
                last_resource_exhausted: bool, stop_requested: bool,
                daemon_stopping: bool, autopilot: bool) -> StopReason | None:
    """次の mission を始めてよいか。止める理由を返し、始めてよければ `None`。

    停止系 (daemon 停止、利用者の停止、autopilot) は 1 回目も見る。期限・回数・
    失敗による停止は 2 回目以降だけ。期限は等号で止まる。"""
    if daemon_stopping:
        return STOP_SHUTDOWN
    if stop_requested:
        return STOP_STOPPED
    if autopilot:
        return STOP_AUTOPILOT
    if runs == 0:
        return None
    if last_resource_exhausted:
        return STOP_RESOURCE_EXHAUSTED
    if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
        return STOP_CONSECUTIVE_FAILURES
    if runs >= max_runs:
        return STOP_MAX_RUNS
    if mono_now >= deadline_mono:
        return STOP_DEADLINE
    return None


_ONEOFF_HEADER = "## 今回だけの指示"
_ONEOFF_BODY = (
    "この job の間だけ有効な指示です。規律 (制約・出力契約) と矛盾する場合は"
    "規律が優先します。恒久の方針と食い違う場合は、この指示を優先します。"
    "この指示の文を backlog や提案に転記しないでください。")


def render_oneoff_section(comment: str | None) -> str:
    if comment is None:
        return ""
    return f"\n\n{_ONEOFF_HEADER}\n\n{_ONEOFF_BODY}\n\n- {comment}"

"""配備中の指標の棚卸し (どの指標が使われていないか)。

入力は read model が集めた行 (配備中の strategy の参照、承認待ちの依頼、戦略候補の
記録、切替・配備操作の記録) で、FS も DB も読まない純関数。時刻に依らない事実
(`UsageFacts` / `UsageScan`) を組み立てておき、読む時刻を渡して判定 (`classify`) する。
そのため事実は memo でき、判定は読取のたびに今の時刻で行える。

未使用 = 次の 4 条件のどれにも当てはまらない指標。

1. 配備中の strategy の設定から参照されている
2. 承認待ちの依頼から参照されている
3. 直近 K 週に作られた戦略候補の依存として登場する
4. いまの配備世代の開始から K 週たっていない

どれか 1 つでも「当てはまる」と決まれば使用中で、ほかの条件の読み損ねは問わない。当てはまる
条件が無く、読み損ねた条件があれば判定不能、全部が「当てはまらない」と決まったときだけ未使用。
未使用でも、いまの版が改善の提出と確認できるものだけを停止の提案にできる。

戦略候補の依存名は family_key の文字列から構造だけで読む。pair の所属や字句の文法は
検査しない (既存の parser は検査して落とすので流用しない)。
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agentic_fx.plugin.loader import _PLUGIN_NAME_RE
from agentic_fx.plugin.logic_identity import _unescape

# 未使用と見なすまでの週数。境界は「配備から K 週以上」「候補が K 週より新しい」。
K_WEEKS = 8
_WEEK = timedelta(weeks=1)

STATE_USED = "used"
STATE_UNUSED = "unused"
STATE_INDETERMINATE = "indeterminate"

COND_LIVE = "live_strategy"
COND_PENDING = "pending_approval"
COND_CANDIDATE = "recent_candidate"
COND_DEPLOY = "recent_deploy"
CONDITIONS = (COND_LIVE, COND_PENDING, COND_CANDIDATE, COND_DEPLOY)

RESULT_HOLDS = "holds"        # この条件で使用中 (試行期間中を含む)
RESULT_CLEAR = "clear"        # この条件では使用中でない
RESULT_UNKNOWN = "unknown"    # 読めず、決められない

# 条件の判定不能の理由 (固定語)
REASON_SCAN_FAILED = "scan_failed"
REASON_PENDING_SOURCE = "pending_source_unreadable"
REASON_PENDING_REQUEST = "pending_request_unreadable"
REASON_CANDIDATES_SOURCE = "candidates_unreadable"
REASON_KEY_MENTIONED = "key_mentioned"
REASON_TIME_UNREADABLE = "time_unreadable"
REASON_JOURNAL_SOURCE = "journal_unreadable"
REASON_NO_GENERATION = "no_generation_record"
REASON_GENERATION_MISMATCH = "generation_mismatch"

# 棚卸し全体の状態
INVENTORY_OK = "ok"
INVENTORY_INDETERMINATE = "indeterminate"

# 除外した候補行の理由 (固定語)
EXCLUDED_NULL_KEY = "null_key"
EXCLUDED_UNREADABLE_KEY = "unreadable_key"
EXCLUDED_UNREADABLE_TIME = "unreadable_time"
EXCLUDED_REASONS = (EXCLUDED_NULL_KEY, EXCLUDED_UNREADABLE_KEY, EXCLUDED_UNREADABLE_TIME)

ORIGIN_STAGING = "staging"
ORIGIN_HUMAN = "human"
ORIGIN_UNKNOWN = "unknown"

NOT_PROPOSABLE_HUMAN = "human_origin"
NOT_PROPOSABLE_UNKNOWN = "unknown_origin"

_RUN = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class CandidateRow:
    id: int
    family_key: str | None
    created_at: str | None


def parse_time(value: object) -> datetime | None:
    """時差つきの ISO 日時だけを読む (時差なしは時刻の意味が決まらないので読まない)。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- 戦略候補の依存名

def _is_name(value: object) -> bool:
    return isinstance(value, str) and _PLUGIN_NAME_RE.fullmatch(value) is not None


def dependency_names(key: str) -> tuple[str, ...] | None:
    """family_key から依存 plugin の名前だけを構造で読む。

    ``strategy:<名前,...>@<足>@<pair,...>`` と ``strategy-v2:<足>@<pair,...>@<依存,...>``
    (依存は ``alias;plugin;mode;symbol;足``) を読み、足・pair・alias・mode・symbol は
    読まない。名前が 1 件も取れない・名前の文法に外れる要素がある・区切りが合わないときは
    None (読めない)。``indicator:`` の key は戦略候補ではないので空の組を返す。"""
    if key.startswith("indicator:"):
        return ()
    if key.startswith("strategy-v2:"):
        parts = key[len("strategy-v2:"):].split("@")
        if len(parts) != 3:
            return None
        names: list[str] = []
        for token in parts[2].split(","):
            fields = token.split(";")
            if len(fields) != 5:
                return None
            plugin = _unescape(fields[1])
            if not _is_name(plugin):
                return None
            names.append(plugin)
        return tuple(dict.fromkeys(names)) if names else None
    if key.startswith("strategy:"):
        parts = key[len("strategy:"):].split("@")
        if len(parts) != 3:
            return None
        names = parts[0].split(",")
        if not names or not all(_is_name(name) for name in names):
            return None
        return tuple(dict.fromkeys(names))
    return None


def mentioned_names(key: str, live: Collection[str]) -> frozenset[str]:
    """読めない key の中に、独立した字句として現れる live の名前。

    ``[a-z0-9_]`` の連なりを 1 語と見て、語全体が名前に一致するものだけを拾う
    (``rsi`` は ``rsi_fast`` の一部としては現れない)。"""
    return frozenset(word for word in _RUN.findall(key) if word in live)


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    created_at: datetime | None
    reason: str | None                 # 除外した理由 (読めたときは None)
    names: frozenset[str]              # 読めた依存名 (時刻も読めた行だけ)
    possible: frozenset[str]           # 使われているかもしれない名前 (読み切れない行)
    indicator: bool = False            # 指標の候補と判別できた行 (戦略候補の件数に入れない)


def parse_candidates(rows: Iterable[CandidateRow], live: Collection[str]
                     ) -> tuple[CandidateRecord, ...]:
    records: list[CandidateRecord] = []
    for row in rows:
        created = parse_time(row.created_at)
        key = row.family_key
        if not isinstance(key, str):
            records.append(CandidateRecord(
                created, EXCLUDED_NULL_KEY, frozenset(), frozenset()))
            continue
        names = dependency_names(key)
        if key.startswith("indicator:"):
            records.append(CandidateRecord(
                created, None, frozenset(), frozenset(), True))
        elif names is None:
            records.append(CandidateRecord(
                created, EXCLUDED_UNREADABLE_KEY, frozenset(), mentioned_names(key, live)))
        elif created is None:
            records.append(CandidateRecord(
                created, EXCLUDED_UNREADABLE_TIME, frozenset(), frozenset(names)))
        else:
            records.append(CandidateRecord(created, None, frozenset(names), frozenset()))
    return tuple(records)


# ---------------------------------------------------------------- 配備世代の開始

@dataclass(frozen=True, slots=True)
class Transition:
    """配備を実際に変えた成功操作 1 件 (切替の journal か、rollback の配備操作)。"""
    at: str | None
    artifact_hash: str
    order: tuple[int, int]             # (source, op_id)。同時刻の並べ替えだけに使う


def generation_start(transitions: Sequence[Transition], live_artifact: str | None
                     ) -> tuple[datetime | None, str | None]:
    """いまの版に至った最後の遷移の時刻。読めないときは (None, 理由)。

    最後の遷移がいまの版を指していない・遷移が無い・時刻が読めないものは、配備時刻不明。"""
    if live_artifact is None or not transitions:
        return None, REASON_NO_GENERATION
    parsed: list[tuple[datetime, tuple[int, int], Transition]] = []
    for item in transitions:
        when = parse_time(item.at)
        if when is None:
            return None, REASON_TIME_UNREADABLE
        parsed.append((when, item.order, item))
    parsed.sort(key=lambda entry: (entry[0], entry[1]))
    when, _order, last = parsed[-1]
    if last.artifact_hash != live_artifact:
        return None, REASON_GENERATION_MISMATCH
    return when, None


# ---------------------------------------------------------------- 事実と判定

@dataclass(frozen=True, slots=True)
class UsageFacts:
    """時刻に依らない、指標 1 本の事実。"""
    name: str
    live_strategy: bool
    pending: str                       # holds | clear | unknown
    pending_reason: str | None
    deployed_at: datetime | None
    deployed_reason: str | None
    origin: str                        # staging | human | unknown
    scan_failed: bool = False


@dataclass(frozen=True, slots=True)
class UsageScan:
    """棚卸し全体の、時刻に依らない事実。"""
    status: str = INVENTORY_OK
    reason: str | None = None
    records: tuple[CandidateRecord, ...] = ()
    candidates_readable: bool = True
    journal_readable: bool = True
    # 名前 -> 読めた依存として登場した候補のうち最も新しい作成時刻
    latest_used: Mapping[str, datetime] = field(default_factory=dict)
    # 名前 -> 使われているかもしれない候補の最も新しい作成時刻
    latest_possible: Mapping[str, datetime] = field(default_factory=dict)
    # 作成時刻が読めない行に使われているかもしれない名前
    possible_untimed: frozenset[str] = frozenset()


def build_scan(records: Sequence[CandidateRecord], *, candidates_readable: bool,
               journal_readable: bool, scan_failed: bool) -> UsageScan:
    used: dict[str, datetime] = {}
    possible: dict[str, datetime] = {}
    untimed: set[str] = set()
    for record in records:
        if record.created_at is not None:
            for name in record.names:
                if name not in used or record.created_at > used[name]:
                    used[name] = record.created_at
            for name in record.possible:
                if name not in possible or record.created_at > possible[name]:
                    possible[name] = record.created_at
        else:
            untimed.update(record.possible)
    status, reason = INVENTORY_OK, None
    if scan_failed:
        status, reason = INVENTORY_INDETERMINATE, REASON_SCAN_FAILED
    elif not journal_readable:
        status, reason = INVENTORY_INDETERMINATE, REASON_JOURNAL_SOURCE
    return UsageScan(status, reason, tuple(records), candidates_readable, journal_readable,
                     used, possible, frozenset(untimed))


def window_start(now: datetime) -> datetime:
    return now - K_WEEKS * _WEEK


def window_summary(scan: UsageScan, now: datetime) -> dict:
    """直近 K 週の戦略候補の件数と、除外した行の理由別の件数。休止の週は 0 件として出る。

    指標の候補は数えない。候補の記録を読めないときの件数は None (0 件と区別する)。"""
    start = window_start(now)
    inside = 0
    excluded = dict.fromkeys(EXCLUDED_REASONS, 0)
    for record in scan.records:
        if (record.created_at is not None and record.created_at >= start
                and not record.indicator):
            inside += 1
        if record.reason is not None and (
                record.created_at is None or record.created_at >= start):
            excluded[record.reason] += 1
    return {"status": scan.status, "reason": scan.reason, "k_weeks": K_WEEKS,
            "candidates_readable": scan.candidates_readable,
            "window_candidates": inside if scan.candidates_readable else None,
            "excluded": excluded}


def _candidate_condition(name: str, scan: UsageScan, start: datetime
                         ) -> tuple[str, str | None]:
    if not scan.candidates_readable:
        return RESULT_UNKNOWN, REASON_CANDIDATES_SOURCE
    used = scan.latest_used.get(name)
    if used is not None and used >= start:
        return RESULT_HOLDS, None
    if name in scan.possible_untimed:
        return RESULT_UNKNOWN, REASON_TIME_UNREADABLE
    possible = scan.latest_possible.get(name)
    if possible is not None and possible >= start:
        return RESULT_UNKNOWN, REASON_KEY_MENTIONED
    return RESULT_CLEAR, None


def _deploy_condition(facts: UsageFacts, now: datetime) -> tuple[str, str | None]:
    if facts.deployed_at is None:
        return RESULT_UNKNOWN, facts.deployed_reason or REASON_NO_GENERATION
    if now - facts.deployed_at >= K_WEEKS * _WEEK:
        return RESULT_CLEAR, None
    return RESULT_HOLDS, None


def classify(facts: UsageFacts, scan: UsageScan, now: datetime) -> dict:
    """指標 1 本の判定。``now`` は読む時刻 (週次の評価なら評価時刻)。

    どれか 1 条件で「当てはまる」と決まれば、ほかの条件が読めなくても使用中。"""
    start = window_start(now)
    if facts.live_strategy:
        live: tuple[str, str | None] = (RESULT_HOLDS, None)
    elif facts.scan_failed:
        live = (RESULT_UNKNOWN, REASON_SCAN_FAILED)
    else:
        live = (RESULT_CLEAR, None)
    results = (live, (facts.pending, facts.pending_reason),
               _candidate_condition(facts.name, scan, start), _deploy_condition(facts, now))
    words = [result for result, _reason in results]
    if RESULT_HOLDS in words:
        state = STATE_USED
    elif RESULT_UNKNOWN in words:
        state = STATE_INDETERMINATE
    else:
        state = STATE_UNUSED
    proposable = state == STATE_UNUSED and facts.origin == ORIGIN_STAGING
    not_proposable = None
    if state == STATE_UNUSED and not proposable:
        not_proposable = (NOT_PROPOSABLE_HUMAN if facts.origin == ORIGIN_HUMAN
                          else NOT_PROPOSABLE_UNKNOWN)
    return {
        "state": state,
        "conditions": [{"id": cond, "result": result, "reason": reason}
                       for cond, (result, reason) in zip(CONDITIONS, results)],
        "origin": facts.origin, "proposable": proposable, "not_proposable": not_proposable,
        "k_weeks": K_WEEKS}

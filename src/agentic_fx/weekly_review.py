"""週次の稼働中戦略の評価と確定。

窓の終端 (週の最後の取引日の終端) で切った戦略別の機械採点 (`signal_forward`) を、
符号と信頼性の 2 軸で判定し、配備中の指標の棚卸し (`usage_review`) と合わせて、週の記録と
集計表に 1 transaction で確定する。評価時刻は実行した時刻で、shadow の終端と棚卸しの
基準になる。採点は LLM の reasoning を読まず、結果は data/ の表と親専有の report にだけ
置く (agent の入力・RAG・取引 prompt に結ばない)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import dataclasses
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from agentic_fx.core import review_weeks
from agentic_fx.plugin import read_model, usage_review
from agentic_fx.plugin.read_model import ReadInputs
from agentic_fx.store import signal_forward
from agentic_fx.store import weekly_review as store
from agentic_fx.store.signal_forward import ForwardTarget

_log = logging.getLogger(__name__)

SIGN_INDETERMINATE = "indeterminate"
SIGN_INSUFFICIENT = "insufficient"
SIGN_STOP_CANDIDATE = "stop_candidate"
SIGN_NEAR_ZERO = "near_zero"
SIGN_ABOVE_FLOOR = "above_floor"

DISPLAY_NEEDS_REVIEW = "needs_review"

REASON_NO_RECENT_DECISIONS = "no_recent_decisions"
REASON_OVERDUE = "overdue_unterminated"
REASON_DEADLINE_UNKNOWN = "deadline_unknown"
REASON_AGED_INSUFFICIENT = "generation_aged_insufficient"

# 劣化の理由 (固定語)
DEGRADED_STRATEGY_UNDETERMINED = "strategy_undetermined"
DEGRADED_STRATEGY_FAILED = "strategy_failed"
DEGRADED_INVENTORY = "inventory_unavailable"
DEGRADED_TARGETS = "targets_unavailable"

REASON_APPROVAL_UNREADABLE = "approval_unreadable"

MIN_SAMPLE = signal_forward.REFERENCE_COUNT      # 30 件
MIN_SPAN = timedelta(days=28)
ZERO_BAND = 0.05
RECENT_DECISION_WINDOW = signal_forward.RECENT_DECISION_WEEKS * timedelta(weeks=1)
GENERATION_LIMIT = signal_forward.WINDOW_WEEKS * timedelta(weeks=1)


# ---------------------------------------------------------------- 判定

@dataclass(frozen=True, slots=True)
class Judgement:
    sign: str
    reasons: list = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """要確認の本数に数えるか。ゼロ近傍、または要確認の理由がある。停止候補との重複を許す。"""
        return self.sign == SIGN_NEAR_ZERO or bool(self.reasons)

    @property
    def display(self) -> str:
        """表示の判定。符号を基本に、ゼロ近傍と、理由つきの床超え候補は要確認にする。"""
        if self.sign == SIGN_NEAR_ZERO or (self.sign == SIGN_ABOVE_FLOOR and self.reasons):
            return DISPLAY_NEEDS_REVIEW
        return self.sign


def _time(value: object) -> datetime | None:
    return usage_review.parse_time(value)


def judge(summary: dict, *, end: datetime) -> Judgement:
    """戦略 1 本の集計から、符号と信頼性の要確認の理由を決める。"""
    if summary.get("state") != signal_forward.STATE_OK:
        return Judgement(SIGN_INDETERMINATE)
    series = summary["series"]["all"]
    n, avg = series["n"], series["avg_r"]
    first = _time(summary.get("first_decided_at"))
    started = _time(summary["generation"]["started_at"])
    if n < MIN_SAMPLE or first is None or end - first < MIN_SPAN:
        sign = SIGN_INSUFFICIENT
    elif avg is None:
        sign = SIGN_INDETERMINATE
    elif avg < -ZERO_BAND:
        sign = SIGN_STOP_CANDIDATE
    elif abs(avg) <= ZERO_BAND:
        sign = SIGN_NEAR_ZERO
    else:
        sign = SIGN_ABOVE_FLOOR
    reasons = []
    if summary.get("recent_decisions", 0) == 0:
        reasons.append(REASON_NO_RECENT_DECISIONS)
    if summary["holes"].get("overdue_unterminated", 0) > 0:
        reasons.append(REASON_OVERDUE)
    if summary["holes"].get("deadline_unknown", 0) > 0:
        reasons.append(REASON_DEADLINE_UNKNOWN)
    if n < MIN_SAMPLE and started is not None and end - started >= GENERATION_LIMIT:
        reasons.append(REASON_AGED_INSUFFICIENT)
    return Judgement(sign, reasons)


# ---------------------------------------------------------------- 窓の終端時点の対象

class TargetsUnavailable(Exception):
    """承認・切替・配備操作の記録を読めず、窓の終端時点の対象を復元できない。"""


@dataclass(slots=True)
class TargetsAsOf:
    targets: list = field(default_factory=list)
    stopped: list = field(default_factory=list)
    # (名前, 内容 hash) -> 承認時の成績 (pair ごとの件数・PF・平均 R)
    approval_metrics: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Event:
    at: datetime | None
    order: tuple[int, int]
    live: bool
    artifact: str | None
    content_hash: str | None
    approval_id: int | None


def _payload(row) -> dict | None:
    try:
        body = json.loads(row.payload_json)
    except (TypeError, ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: object) -> float | int | None:
    if type(value) in (int, float) and value == value and abs(value) != float("inf"):
        return value
    return None


def _metrics(payload: dict | None) -> dict | None:
    raw = payload.get("metrics") if payload else None
    if not isinstance(raw, dict):
        return None
    pairs = {}
    for pair, entry in sorted(raw.items()):
        if isinstance(pair, str) and isinstance(entry, dict):
            pairs[pair] = {key: _number(entry.get(key)) for key in ("trades", "pf", "avg_r")
                           if _number(entry.get(key)) is not None}
    return {"pairs": pairs} if pairs else None


def targets_as_of(inputs: ReadInputs, end: datetime) -> TargetsAsOf:
    """窓の終端時点で配備中だった戦略と、止まっていた名前。

    配備を実際に変えた成功操作 (切替の決定、rollback) と、停止・退避の完了を時刻順に
    並べ、終端以前の最後の操作で決める。同時刻は (source, op_id) の順。操作の時刻や
    承認を読めない名前は、読めない理由つきの対象にして数値を出さない。"""
    if (inputs.journal_status == read_model.SOURCE_UNAVAILABLE
            or inputs.pending_status == read_model.SOURCE_UNAVAILABLE):
        raise TargetsUnavailable("approval or deployment records unreadable")
    payloads: dict[int, dict | None] = {row.id: _payload(row) for row in inputs.approvals}
    by_artifact: dict[tuple[str, str], dict] = {}
    kinds: dict[str, str] = {}
    for body in payloads.values():
        if body is None:
            continue
        name, kind, artifact = (_str(body.get("name")), _str(body.get("kind")),
                                _str(body.get("artifact_hash")))
        if name is not None and kind in read_model.PLUGIN_KINDS:
            kinds[name] = kind
            if artifact is not None:
                by_artifact[(name, artifact)] = body
    events: dict[str, list[_Event]] = {}
    for journal in inputs.journals:
        if (journal.phase == "decided" and journal.kind in ("approve", "bless")
                and journal.switch_required):
            body = payloads.get(journal.approval_id)
            events.setdefault(journal.name, []).append(_Event(
                _time(journal.updated_at), (usage_review.SOURCE_JOURNAL, journal.op_id), True,
                str(journal.new_target).rsplit("/", 1)[-1],
                _str(body.get("content_hash")) if body else None, journal.approval_id))
    for item in inputs.deploy_ops:
        if item.phase != "done":
            continue
        order = (usage_review.SOURCE_DEPLOY_OP, item.op_id)
        if item.op_kind == "rollback_symlink" and item.target_artifact_hash is not None:
            events.setdefault(item.name, []).append(_Event(
                _time(item.updated_at), order, True, item.target_artifact_hash,
                item.target_content_hash, None))
        elif item.op_kind in ("disable_symlink", "retire_plain"):
            events.setdefault(item.name, []).append(_Event(
                _time(item.updated_at), order, False, None, None, None))
    out = TargetsAsOf()
    for name in sorted(events):
        items = events[name]
        if any(event.at is None for event in items):
            if kinds.get(name, read_model.KIND_STRATEGY) == read_model.KIND_STRATEGY:
                out.targets.append(ForwardTarget(
                    name, "", None, None, usage_review.REASON_TIME_UNREADABLE))
            continue
        seen = sorted((event for event in items if event.at <= end),
                      key=lambda event: (event.at, event.order))
        if not seen:
            continue
        last = seen[-1]
        if not last.live:
            out.stopped.append({"name": name, "kind": kinds.get(name)})
            continue
        body = by_artifact.get((name, last.artifact or ""))
        if body is None and last.approval_id is not None:
            body = payloads.get(last.approval_id)
        kind = (_str(body.get("kind")) if body else None) or kinds.get(name)
        content = last.content_hash or (_str(body.get("content_hash")) if body else None)
        if kind not in (None, read_model.KIND_STRATEGY):
            continue
        if kind is None or content is None:
            out.targets.append(ForwardTarget(name, "", None, None, REASON_APPROVAL_UNREADABLE))
            continue
        out.targets.append(ForwardTarget(name, content, last.at, last.order, None))
        metrics = _metrics(body)
        if metrics is not None:
            out.approval_metrics[(name, content)] = metrics
    known = {target.name for target in out.targets}
    for entry in inputs.live.entries:
        meta = entry.meta
        name = meta.name if meta is not None else entry.entry_name
        kind = meta.kind if meta is not None else kinds.get(name, read_model.KIND_STRATEGY)
        if name in events or name in known or kind != read_model.KIND_STRATEGY:
            continue
        # 配備中なのに配備の記録が 1 件も無い (記録を残さない配置)。いつから現世代かを
        # 決められないので、数値を出さず判定不能の対象にする。
        known.add(name)
        out.targets.append(ForwardTarget(
            name, meta.content_hash if meta is not None else "", None, None,
            usage_review.REASON_NO_GENERATION))
    out.targets.sort(key=lambda target: target.name)
    return out


# ---------------------------------------------------------------- 棚卸し

def _inventory(inputs: ReadInputs, settings, evaluated_at: datetime):
    """評価時刻での配備中の指標の棚卸し。(状態, 理由, 未使用・判定不能の行)。"""
    model = read_model.build_read_model(inputs, settings=settings)
    listing = read_model.model_to_dto(model, now=evaluated_at)
    ok = model.usage.status == usage_review.INVENTORY_OK
    rows = []
    for plugin in listing["plugins"]:
        review = plugin.get("usage_review")
        if review is None or review["state"] not in (usage_review.STATE_UNUSED,
                                                     usage_review.STATE_INDETERMINATE):
            continue
        rows.append(store.InventoryRow(
            plugin["name"], review["state"], review["origin"], review["conditions"],
            bool(review["proposable"]), review["not_proposable"]))
    return (store.INVENTORY_NORMAL if ok else store.INVENTORY_INDETERMINATE,
            None if ok else model.usage.reason, rows)


# ---------------------------------------------------------------- 評価

@dataclass(slots=True)
class Evaluation:
    record: store.WeekRecord
    strategies: list
    inventory: list


_RETRYABLE_WORDS = ("locked", "busy", "interrupted")


def _propagate_retryable(exc: Exception) -> None:
    """競合・中断は行の失敗にせず送出する (週の確定を試行ごとやり直せる)。"""
    if isinstance(exc, sqlite3.OperationalError) and any(
            word in str(exc).lower() for word in _RETRYABLE_WORDS):
        raise exc


def _strategy_row(summary: dict, target: ForwardTarget, judgement: Judgement,
                  approval: dict | None) -> store.StrategyRow:
    ok = summary["state"] == signal_forward.STATE_OK
    generation = target.generation if target.generation is not None else (-1, -1)
    base = dict(name=target.name, content_hash=target.content_hash,
                generation_source=generation[0], generation_op_id=generation[1],
                generation_started_at=target.started_at, approval=approval,
                sign=judgement.sign, reasons=list(judgement.reasons))
    if not ok:
        return store.StrategyRow(
            state=summary["state"], state_reason=summary.get("reason"), first_decided_at=None,
            n=0, wins=0, losses=0, flats=0, r_profit=0.0, r_loss=0.0, series={}, holes={},
            decisions={}, concentration={}, short_term={}, **base)
    series = summary["series"]["all"]
    return store.StrategyRow(
        state=summary["state"], state_reason=None,
        first_decided_at=summary["first_decided_at"], n=series["n"], wins=series["wins"],
        losses=series["losses"], flats=series["flats"], r_profit=series["r_profit"],
        r_loss=series["r_loss"], series=summary["series"], holes=summary["holes"],
        decisions={**summary["decisions"], "reach": summary["reach"],
                   "outcomes": summary["outcomes"]},
        concentration=summary["concentration"],
        short_term={**summary["short_term"], "comparison": summary["comparison"]}, **base)


def evaluate_week(conn: sqlite3.Connection, label: date, close_at: datetime, *,
                  evaluated_at: datetime, inputs: ReadInputs, settings) -> Evaluation:
    """週 1 本の評価。例外はそのまま送出する (全体をやり直す)。計算不能として返せる
    結果 (戦略の集計不能、配備時刻不明、棚卸しの判定不能) は劣化として確定する。"""
    degraded: dict[str, int] = {}

    def bump(reason: str) -> None:
        degraded[reason] = degraded.get(reason, 0) + 1

    strategies: list[store.StrategyRow] = []
    stopped: list = []
    try:
        restored = targets_as_of(inputs, close_at)
    except TargetsUnavailable:
        bump(DEGRADED_TARGETS)
        restored = TargetsAsOf()
    stopped = restored.stopped
    for target in restored.targets:
        summary = signal_forward.summary_or_failed(
            conn, target, now=close_at, evaluated_at=evaluated_at, judgement_inputs=True,
            on_error=_propagate_retryable)
        judgement = judge(summary, end=close_at)
        if summary["state"] == signal_forward.STATE_UNDETERMINED:
            bump(DEGRADED_STRATEGY_UNDETERMINED)
        elif summary["state"] == signal_forward.STATE_FAILED:
            bump(DEGRADED_STRATEGY_FAILED)
        strategies.append(_strategy_row(
            summary, target, judgement,
            restored.approval_metrics.get((target.name, target.content_hash))))
    state, reason, inventory = _inventory(inputs, settings, evaluated_at)
    if state != store.INVENTORY_NORMAL:
        bump(DEGRADED_INVENTORY)
    record = store.WeekRecord(
        week=label.isoformat(), window_end=close_at, finalized_at=evaluated_at,
        evaluated_at=evaluated_at, status=store.STATUS_COMPLETED,
        evaluation=store.EVAL_DEGRADED if degraded else store.EVAL_NORMAL,
        degraded=degraded, inventory_state=state, inventory_reason=reason, stopped=stopped)
    return Evaluation(record, strategies, inventory)


def finalize_week(read_conn: sqlite3.Connection, write_conn: sqlite3.Connection,
                  label: date, close_at: datetime, *, now: datetime,
                  read_inputs: Callable[[sqlite3.Connection], ReadInputs],
                  settings, write_scope: Callable | None = None,
                  clock: Callable[[], datetime] | None = None) -> str:
    """週を評価して確定する。確定済みなら何もせず `EXISTS`。評価の例外は送出し、何も書かない。
    `write_scope` は、DB へ書く範囲 (確定の transaction) だけを包む。評価の読取は包まない。
    `clock` があれば、保存の直前に読み直す。確定の時刻はその時刻で、窓の終端から打切りの
    期間を過ぎていれば、評価を捨てて打切りで確定する。"""
    if store.week_exists(read_conn, label.isoformat()):
        return store.EXISTS
    evaluation = evaluate_week(read_conn, label, close_at, evaluated_at=now,
                               inputs=read_inputs(read_conn), settings=settings)
    with write_scope() if write_scope is not None else nullcontext():
        saved_at = clock() if clock is not None else now
        if clock is not None and saved_at > close_at + review_weeks.ABORT_AFTER:
            return abort_week(write_conn, label, close_at, now=saved_at)
        record = dataclasses.replace(evaluation.record, finalized_at=saved_at)
        return store.commit_week(write_conn, record, evaluation.strategies,
                                 evaluation.inventory)


def abort_week(write_conn: sqlite3.Connection, label: date, close_at: datetime, *,
               now: datetime) -> str:
    """確定できないまま期間を過ぎた週を打切りとして記録する。"""
    return store.commit_aborted(write_conn, label.isoformat(), window_end=close_at,
                                finalized_at=now, reason=store.ABORT_PERIOD_EXCEEDED)

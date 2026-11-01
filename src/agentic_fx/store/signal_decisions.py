"""signal の最終採否 (decision) と、採否確定 transaction。

decision は accept (LLM、intent あり)、LLM reject (`news_shock` /
`opposite_position`)、system reject (`econ_window`) の 3 正規形だけで、1 signal
に 1 行。確定は次の書込みを 1 transaction に束ねる。

- accept: intent 記録 → decision → accept shadow → consume → mission completed
- reject: decision → reject shadow → consume → mission 終端 (LLM は completed、
  system は system_filtered)

`*_in_tx` は commit も rollback もしない。`commit_*` は `*_in_tx` を呼んで 1 回だけ
commit し、途中のどの書込みが失敗しても rollback して例外を上げる。commit 前に
接続が transaction 中なら、他人の未 commit の書込みを巻き込まないよう何もせず
拒否する。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from agentic_fx.store import intents, missions, signals
from agentic_fx.store.signal_shadow import ShadowSpread, start_in_tx
from agentic_fx.store.signals import utc_iso

DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"
ACTOR_LLM = "llm"
ACTOR_SYSTEM = "system"
LLM_REJECT_CODES = ("news_shock", "opposite_position")
SYSTEM_REJECT_CODE = "econ_window"
MISSION_COMPLETED = "completed"


class DecisionShapeError(ValueError):
    """3 正規形のどれにも当たらない decision。"""


class DecisionTxError(RuntimeError):
    """採否確定 transaction の中の CAS が合わなかった (mission が running でない等)。"""


@dataclass(frozen=True, slots=True)
class DecisionCommit:
    decision_id: int
    shadow_id: int
    trade_intent_id: int | None
    mission_status: str


def validate_shape(*, decision: str, actor: str, reject_code: str | None,
                   trade_intent_id: int | None) -> None:
    """DB の CHECK と同じ 3 正規形を SQL の前に検査する。"""
    if decision == DECISION_ACCEPT:
        ok = (actor == ACTOR_LLM and reject_code is None
              and trade_intent_id is not None)
    elif decision == DECISION_REJECT and actor == ACTOR_LLM:
        ok = reject_code in LLM_REJECT_CODES and trade_intent_id is None
    elif decision == DECISION_REJECT and actor == ACTOR_SYSTEM:
        ok = reject_code == SYSTEM_REJECT_CODE and trade_intent_id is None
    else:
        ok = False
    if not ok:
        raise DecisionShapeError(
            f"decision={decision!r} actor={actor!r} reject_code={reject_code!r} "
            f"trade_intent={'set' if trade_intent_id is not None else 'null'} "
            "is not one of the three canonical forms")


def _check_intent(conn: sqlite3.Connection, trade_intent_id: int,
                  mission_id: int) -> None:
    row = conn.execute(
        "SELECT mission_id, action FROM trade_intents WHERE id=?",
        (trade_intent_id,)).fetchone()
    if row is None:
        raise DecisionShapeError(f"trade intent {trade_intent_id} does not exist")
    if row[0] != mission_id:
        raise DecisionShapeError(
            f"trade intent {trade_intent_id} belongs to another mission")
    if row[1] != "open":
        raise DecisionShapeError(
            f"trade intent {trade_intent_id} is not an open intent")
    used = conn.execute(
        "SELECT 1 FROM signal_decisions WHERE trade_intent_id=?",
        (trade_intent_id,)).fetchone()
    if used is not None:
        raise DecisionShapeError(
            f"trade intent {trade_intent_id} is already bound to a decision")


def record_in_tx(conn: sqlite3.Connection, *, mission_id: int, signal_id: int,
                 decision: str, actor: str, reject_code: str | None,
                 trade_intent_id: int | None, now: datetime) -> int:
    """decision 1 行を書く。commit しない。

    accept の intent は同じ mission の open intent で、他の decision に結ばれて
    いないことを確かめる。signal の一意性は DB の UNIQUE が守る。
    """
    validate_shape(decision=decision, actor=actor, reject_code=reject_code,
                   trade_intent_id=trade_intent_id)
    if trade_intent_id is not None:
        _check_intent(conn, trade_intent_id, mission_id)
    cur = conn.execute(
        "INSERT INTO signal_decisions (mission_id,signal_id,trade_intent_id,"
        "decision,actor,reject_code,decided_at) VALUES (?,?,?,?,?,?,?)",
        (mission_id, signal_id, trade_intent_id, decision, actor, reject_code,
         utc_iso(now)))
    return cur.lastrowid


def _consume_and_finish(conn: sqlite3.Connection, *, mission_id: int,
                        signal_id: int, actor: str, output: dict | None,
                        transcript: list, now: datetime) -> str:
    signals.consume_in_tx(conn, signal_id, mission_id=mission_id, now=now)
    if actor == ACTOR_SYSTEM:
        status = missions.STATUS_SYSTEM_FILTERED
        ok = missions.finish_system_filtered_in_tx(
            conn, mission_id, output, transcript, now)
    else:
        status = MISSION_COMPLETED
        ok = missions.finish_in_tx(conn, mission_id, status, output,
                                   transcript, now)
    if not ok:
        raise DecisionTxError(f"mission {mission_id} is not running")
    return status


def finalize_reject_in_tx(conn: sqlite3.Connection, *, mission_id: int,
                          signal_id: int, actor: str, reject_code: str,
                          spread: ShadowSpread, output: dict | None,
                          transcript: list, now: datetime) -> DecisionCommit:
    """reject (LLM / system) の decision・reject shadow・consume・mission 終端。"""
    decision_id = record_in_tx(
        conn, mission_id=mission_id, signal_id=signal_id,
        decision=DECISION_REJECT, actor=actor, reject_code=reject_code,
        trade_intent_id=None, now=now)
    shadow_id = start_in_tx(conn, decision_id=decision_id, spread=spread, now=now)
    status = _consume_and_finish(
        conn, mission_id=mission_id, signal_id=signal_id, actor=actor,
        output=output, transcript=transcript, now=now)
    return DecisionCommit(decision_id, shadow_id, None, status)


def finalize_accept_in_tx(conn: sqlite3.Connection, *, mission_id: int,
                          signal_id: int, intent_payload: dict,
                          spread: ShadowSpread, output: dict | None,
                          transcript: list, now: datetime) -> DecisionCommit:
    """accept の intent・decision・accept shadow・consume・mission completed。

    intent はこの transaction の中で作り、その id を decision に結ぶ。executor は
    この transaction の commit 後にだけ呼ぶ (呼び出し側の責務)。
    """
    if intent_payload.get("action", "open") != "open":
        raise DecisionShapeError(
            f"an accept intent is an open intent, got {intent_payload['action']!r}")
    intent_id = intents.insert_in_tx(conn, mission_id, intent_payload, now,
                                     action="open")
    decision_id = record_in_tx(
        conn, mission_id=mission_id, signal_id=signal_id,
        decision=DECISION_ACCEPT, actor=ACTOR_LLM, reject_code=None,
        trade_intent_id=intent_id, now=now)
    shadow_id = start_in_tx(conn, decision_id=decision_id, spread=spread, now=now)
    status = _consume_and_finish(
        conn, mission_id=mission_id, signal_id=signal_id, actor=ACTOR_LLM,
        output=output, transcript=transcript, now=now)
    return DecisionCommit(decision_id, shadow_id, intent_id, status)


def _commit_once(conn: sqlite3.Connection, fn, kwargs) -> DecisionCommit:
    if conn.in_transaction:
        raise RuntimeError(
            "connection already has an open transaction; refusing to commit "
            "or roll back writes that are not part of the signal decision")
    try:
        result = fn(conn, **kwargs)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return result


def commit_reject(conn: sqlite3.Connection, **kwargs) -> DecisionCommit:
    """`finalize_reject_in_tx` を 1 transaction として commit する。"""
    return _commit_once(conn, finalize_reject_in_tx, kwargs)


def commit_accept(conn: sqlite3.Connection, **kwargs) -> DecisionCommit:
    """`finalize_accept_in_tx` を 1 transaction として commit する。"""
    return _commit_once(conn, finalize_accept_in_tx, kwargs)


def get_for_signal(conn: sqlite3.Connection, signal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM signal_decisions WHERE signal_id=?",
                       (signal_id,)).fetchone()
    return dict(row) if row else None

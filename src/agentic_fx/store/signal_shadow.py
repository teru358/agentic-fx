"""signal の採否ごとに 1 行作る仮想約定 (shadow) の store 操作。

行の作成は採否確定 transaction の中で `start_in_tx` が行い、decision 行から
decision / actor / reject_code を写す (別値を渡す経路を持たない)。以後の状態は
`_TRANSITIONS` の表にある遷移だけを `state` と `updated_at` の CAS で 1 回だけ
書く。終端 (`resolved` / `expired`) からの遷移は無い。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agentic_fx.store.short_tx import (
    Begin, Checkpoint, Conflict, TxResult, begin_immediate, short_tx)
from agentic_fx.store.signals import utc_iso

RULE_VERSION = 1

STATE_PENDING_ENTRY = "pending_entry"
STATE_FILLED = "filled"
STATE_RESOLVED = "resolved"
STATE_EXPIRED = "expired"
STATES = (STATE_PENDING_ENTRY, STATE_FILLED, STATE_RESOLVED, STATE_EXPIRED)

RESULT_SL = "sl"
RESULT_TP = "tp"
RESULT_DAY_CLOSE = "day_close"
RESULT_DATA_INSUFFICIENT = "data_insufficient"
RESULT_NO_FILL = "no_fill"
NUMERIC_RESULTS = (RESULT_SL, RESULT_TP, RESULT_DAY_CLOSE)

SPREAD_QUOTE = "quote"
SPREAD_ASSUMED = "assumed"

# 遷移表 (from_state, to_state, result_code, fill の要否, exit の要否)。
# fill: "absent" = entry 2 列 NULL、"present" = 2 列とも値、"kept" = filled 行の
# 既存 entry をそのまま残す。exit: exit_price / pnl_pips / resolved_at の 3 列。
_TRANSITIONS = (
    (STATE_PENDING_ENTRY, STATE_FILLED, None, "present", False),
    (STATE_PENDING_ENTRY, STATE_RESOLVED, RESULT_SL, "present", True),
    (STATE_PENDING_ENTRY, STATE_RESOLVED, RESULT_TP, "present", True),
    (STATE_PENDING_ENTRY, STATE_RESOLVED, RESULT_DAY_CLOSE, "present", True),
    (STATE_PENDING_ENTRY, STATE_RESOLVED, RESULT_DATA_INSUFFICIENT, "absent",
     False),
    (STATE_PENDING_ENTRY, STATE_RESOLVED, RESULT_DATA_INSUFFICIENT, "present",
     False),
    (STATE_PENDING_ENTRY, STATE_EXPIRED, RESULT_NO_FILL, "absent", False),
    (STATE_FILLED, STATE_RESOLVED, RESULT_SL, "kept", True),
    (STATE_FILLED, STATE_RESOLVED, RESULT_TP, "kept", True),
    (STATE_FILLED, STATE_RESOLVED, RESULT_DAY_CLOSE, "kept", True),
    (STATE_FILLED, STATE_RESOLVED, RESULT_DATA_INSUFFICIENT, "kept", False),
)


class ShadowTransitionError(ValueError):
    """遷移表に無い遷移、または列の組が表と合わない。"""


@dataclass(frozen=True, slots=True)
class ShadowSpread:
    """decision 時点の規則 snapshot。"""
    pip_size: float
    spread_pips: float
    source: str

    def __post_init__(self) -> None:
        if not (isinstance(self.pip_size, (int, float))
                and math.isfinite(self.pip_size) and self.pip_size > 0):
            raise ValueError(f"pip_size must be finite and > 0: {self.pip_size!r}")
        if not (isinstance(self.spread_pips, (int, float))
                and math.isfinite(self.spread_pips) and self.spread_pips >= 0):
            raise ValueError(
                f"spread_pips must be finite and >= 0: {self.spread_pips!r}")
        if self.source not in (SPREAD_QUOTE, SPREAD_ASSUMED):
            raise ValueError(f"unknown spread_source: {self.source!r}")


def spread_from_quote(bid: float | None, ask: float | None, *, pip_size: float,
                      assumed_spread_pips: float) -> ShadowSpread:
    """quote が有限で `ask > bid > 0` なら quote 由来、それ以外は assumed。

    鮮度や取得失敗の判定は呼び出し側の quote 経路が持ち、使えない quote は
    `None` で渡す。bid = ask は assumed になる。
    """
    usable = (bid is not None and ask is not None
              and isinstance(bid, (int, float)) and isinstance(ask, (int, float))
              and math.isfinite(bid) and math.isfinite(ask)
              and ask > bid > 0)
    if usable:
        return ShadowSpread(pip_size=pip_size,
                            spread_pips=(ask - bid) / pip_size,
                            source=SPREAD_QUOTE)
    return ShadowSpread(pip_size=pip_size, spread_pips=assumed_spread_pips,
                        source=SPREAD_ASSUMED)


def observation_start(decided_at: datetime) -> datetime:
    """decision 時刻を次の UTC 分境界へ切り上げた観測開始 t0。

    分境界ちょうどならその時刻を返す。naive は拒否する。
    """
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise ValueError("decided_at is naive; tz-aware datetime required")
    utc = decided_at.astimezone(timezone.utc)
    floor = utc.replace(second=0, microsecond=0)
    return floor if floor == utc else floor + timedelta(minutes=1)


def pnl_pips(direction: str, *, entry: float, exit_price: float,
             pip_size: float) -> float:
    """long は (exit-entry)/pip、short は (entry-exit)/pip。手数料等は含めない。"""
    if direction == "long":
        return (exit_price - entry) / pip_size
    if direction == "short":
        return (entry - exit_price) / pip_size
    raise ValueError(f"unknown direction: {direction!r}")


def start_in_tx(conn: sqlite3.Connection, *, decision_id: int,
                spread: ShadowSpread, now: datetime) -> int:
    """decision 行から snapshot を写して `pending_entry` の shadow を作る。

    commit しない。decision が無ければ `LookupError`。
    """
    stamp = utc_iso(now)
    cur = conn.execute(
        "INSERT INTO signal_shadow (decision_id,signal_id,decision,actor,"
        "reject_code,state,pip_size,spread_pips,spread_source,rule_version,"
        "created_at,updated_at) "
        "SELECT id,signal_id,decision,actor,reject_code,?,?,?,?,?,?,? "
        "FROM signal_decisions WHERE id=?",
        (STATE_PENDING_ENTRY, float(spread.pip_size), float(spread.spread_pips),
         spread.source, RULE_VERSION, stamp, stamp, decision_id))
    if cur.rowcount != 1:
        raise LookupError(f"signal decision {decision_id} does not exist")
    return cur.lastrowid


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """書き込む遷移先。`entry_*` は fill を伴う遷移だけ、`exit_*` は数値終端だけ。"""
    state: str
    result_code: str | None = None
    entry_price: float | None = None
    entry_at: datetime | None = None
    exit_price: float | None = None
    pnl_pips: float | None = None
    resolved_at: datetime | None = None

    @classmethod
    def filled(cls, *, entry_price: float, entry_at: datetime) -> "ShadowOutcome":
        return cls(STATE_FILLED, None, entry_price, entry_at)

    @classmethod
    def resolved(cls, result_code: str, *, exit_price: float, pnl_pips: float,
                 resolved_at: datetime, entry_price: float | None = None,
                 entry_at: datetime | None = None) -> "ShadowOutcome":
        return cls(STATE_RESOLVED, result_code, entry_price, entry_at,
                   exit_price, pnl_pips, resolved_at)

    @classmethod
    def data_insufficient(cls, *, entry_price: float | None = None,
                          entry_at: datetime | None = None) -> "ShadowOutcome":
        return cls(STATE_RESOLVED, RESULT_DATA_INSUFFICIENT, entry_price,
                   entry_at)

    @classmethod
    def no_fill(cls) -> "ShadowOutcome":
        return cls(STATE_EXPIRED, RESULT_NO_FILL)


def _finite(value, label: str) -> float:
    if not (isinstance(value, (int, float)) and math.isfinite(value)):
        raise ShadowTransitionError(f"{label} must be a finite number: {value!r}")
    return float(value)


def check_transition(from_state: str, outcome: ShadowOutcome):
    """遷移表で `(fill の扱い, exit の要否)` を引く。表に無ければ
    `ShadowTransitionError`。"""
    has_fill = outcome.entry_price is not None or outcome.entry_at is not None
    for f, t, code, fill, needs_exit in _TRANSITIONS:
        if f != from_state or t != outcome.state or code != outcome.result_code:
            continue
        if fill == "absent" and has_fill:
            continue
        if fill == "present" and not has_fill:
            continue
        return fill, needs_exit
    raise ShadowTransitionError(
        f"shadow transition {from_state} -> {outcome.state}"
        f"/{outcome.result_code} with fill={has_fill} is not allowed")


def transition_in_tx(conn: sqlite3.Connection, shadow_id: int, *,
                     expected_state: str, expected_updated_at: str,
                     outcome: ShadowOutcome, now: datetime,
                     checkpoint: Checkpoint | None = None) -> bool:
    """`expected_state` / `expected_updated_at` が一致するときだけ遷移を書く。

    表に無い遷移は SQL を出す前に `ShadowTransitionError`。CAS 不一致は
    `False` を返し何も書かない。commit しない。
    """
    fill, needs_exit = check_transition(expected_state, outcome)
    if fill == "kept" and (outcome.entry_price is not None
                           or outcome.entry_at is not None):
        raise ShadowTransitionError(
            "a filled shadow keeps its stored entry; do not pass a new one")
    if fill == "present":
        if outcome.entry_price is None or outcome.entry_at is None:
            raise ShadowTransitionError("entry_price and entry_at go together")
        entry_price = _finite(outcome.entry_price, "entry_price")
        entry_at = utc_iso(outcome.entry_at)
    else:
        entry_price = entry_at = None
    if needs_exit:
        if outcome.resolved_at is None:
            raise ShadowTransitionError("a numeric result needs resolved_at")
        exit_cols = (_finite(outcome.exit_price, "exit_price"),
                     _finite(outcome.pnl_pips, "pnl_pips"),
                     utc_iso(outcome.resolved_at))
    else:
        if (outcome.exit_price is not None or outcome.pnl_pips is not None
                or outcome.resolved_at is not None):
            raise ShadowTransitionError(
                f"{outcome.state}/{outcome.result_code} has no exit values")
        exit_cols = (None, None, None)
    if checkpoint is not None:
        checkpoint()
    entry_sql = ("" if fill == "kept"
                 else "entry_price=:entry_price, entry_at=:entry_at, ")
    cur = conn.execute(
        "UPDATE signal_shadow SET state=:state, result_code=:code, "
        f"{entry_sql}exit_price=:exit_price, pnl_pips=:pnl, "
        "resolved_at=:resolved_at, updated_at=:now "
        "WHERE id=:id AND state=:expected AND updated_at=:expected_at",
        {"state": outcome.state, "code": outcome.result_code,
         "entry_price": entry_price, "entry_at": entry_at,
         "exit_price": exit_cols[0], "pnl": exit_cols[1],
         "resolved_at": exit_cols[2], "now": utc_iso(now), "id": shadow_id,
         "expected": expected_state, "expected_at": expected_updated_at})
    return cur.rowcount == 1


def transition_tx(conn: sqlite3.Connection, shadow_id: int, *,
                  expected_state: str, expected_updated_at: str,
                  outcome: ShadowOutcome, now: datetime,
                  begin: Begin = begin_immediate,
                  checkpoint: Checkpoint | None = None) -> TxResult:
    """`transition_in_tx` を 1 短 transaction で commit する。

    CAS 不一致は `conflict`。開いた transaction の上では呼べない。
    """
    def body():
        if not transition_in_tx(conn, shadow_id, expected_state=expected_state,
                                expected_updated_at=expected_updated_at,
                                outcome=outcome, now=now, checkpoint=checkpoint):
            raise Conflict
        return {}

    return short_tx(conn, begin, body, checkpoint)


def transition(conn: sqlite3.Connection, shadow_id: int, *,
               expected_state: str, expected_updated_at: str,
               outcome: ShadowOutcome, now: datetime, **kwargs) -> bool:
    """`transition_tx` が commit したか。"""
    return transition_tx(conn, shadow_id, expected_state=expected_state,
                         expected_updated_at=expected_updated_at,
                         outcome=outcome, now=now, **kwargs).committed


def get(conn: sqlite3.Connection, shadow_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM signal_shadow WHERE id=?",
                       (shadow_id,)).fetchone()
    return dict(row) if row else None


def unresolved_candidates(conn: sqlite3.Connection, *,
                          decided_before: datetime,
                          limit: int,
                          after: tuple[str, int] | None = None) -> list[dict]:
    """解決候補: 未終端 (`pending_entry` / `filled`) で、slice set が
    complete / unrecoverable でなく、decision が `decided_before` 以前の行。

    最終確定点 (区間終端 + 1 日) は fill / 期限 / day close の規則から worker
    が計算する。この query は「最も早い確定点より前の行は返さない」前段の
    絞り込みだけを行う。古い decision から返す。`decided_at` は utc_iso で
    書かれるので文字列比較で時刻順に比べる。

    `after=(decided_at, shadow_id)` を渡すと、その key より後ろ (同じ順序で
    次) の行から返す。既定 `None` は先頭から。
    """
    cutoff = utc_iso(decided_before)
    keyset, extra = "", ()
    if after is not None:
        keyset = "AND (d.decided_at > ? OR (d.decided_at = ? AND sh.id > ?)) "
        extra = (after[0], after[0], int(after[1]))
    return [dict(r) for r in conn.execute(
        "SELECT sh.*, d.decided_at, d.mission_id, d.trade_intent_id, "
        "s.plugin, s.pair, s.timeframe, s.bar_ts, s.payload_json, "
        "ss.id AS set_id, ss.state AS set_state "
        "FROM signal_shadow sh "
        "JOIN signal_decisions d ON d.id = sh.decision_id "
        "JOIN signals s ON s.id = sh.signal_id "
        "LEFT JOIN ohlcv_slice_sets ss ON ss.shadow_id = sh.id "
        "WHERE sh.state IN (?, ?) "
        "AND (ss.id IS NULL OR ss.state NOT IN ('complete','unrecoverable')) "
        "AND d.decided_at <= ? " + keyset +
        "ORDER BY d.decided_at, sh.id LIMIT ?",
        (STATE_PENDING_ENTRY, STATE_FILLED, cutoff, *extra, int(limit)))]


def period_summary(conn: sqlite3.Connection, *, since: datetime,
                   until: datetime) -> list[dict]:
    """人間面用の期間集計。decision 時刻 `[since, until)` の shadow を strategy・
    採否・actor・reject_code・state・result_code ごとに数える。

    `n` は行数、`numeric_n` / `pnl_pips_sum` / `win_n` は数値終端だけを数える。
    agent 面へ渡さない値であり、渡す経路は呼び出し側が持たない。
    """
    return [dict(r) for r in conn.execute(
        "SELECT s.plugin, sh.decision, sh.actor, sh.reject_code, sh.state, "
        "sh.result_code, COUNT(*) AS n, "
        "SUM(CASE WHEN sh.pnl_pips IS NOT NULL THEN 1 ELSE 0 END) AS numeric_n, "
        "TOTAL(sh.pnl_pips) AS pnl_pips_sum, "
        "SUM(CASE WHEN sh.pnl_pips > 0 THEN 1 ELSE 0 END) AS win_n "
        "FROM signal_shadow sh "
        "JOIN signal_decisions d ON d.id = sh.decision_id "
        "JOIN signals s ON s.id = sh.signal_id "
        "WHERE d.decided_at >= ? AND d.decided_at < ? "
        "GROUP BY s.plugin, sh.decision, sh.actor, sh.reject_code, sh.state, "
        "sh.result_code "
        "ORDER BY s.plugin, sh.decision, sh.actor, sh.reject_code, sh.state, "
        "sh.result_code",
        (utc_iso(since), utc_iso(until)))]

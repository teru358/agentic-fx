"""指標回避帯・signal 採否・shadow・slice 容量の人間向け集計と、親専有の日次 report。

status と日次 report が同じ集計を使う。agent の prompt・tool・worker へは渡さない。
読む接続は呼び出し側が用意した読み取り接続だけで、書くのは report file だけである。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agentic_fx._safe_error import safe_error_text
from agentic_fx.core import market_hours
from agentic_fx.store import ohlcv_slices, signals

_log = logging.getLogger("agentic_fx.loops.econ_signal_report")

#: abandoned を理由別に件数表示する 7 語。cleanup と legacy は別枠。
ABANDON_REASONS = (
    "freshness_expired", "requeued_expired", "retry_exhausted", "source_inactive",
    "normalization_failed", "deploy_op", "deploy_linked")

#: mission の既知の終端。`system_filtered` は帯内の system 却下で、失敗ではない。
KNOWN_MISSION_TERMINALS = ("completed", "failed", "skipped", "system_filtered")

#: byte 比較の対象にする slice 関連の 7 表 (とその index)。
SLICE_RELATED_TABLES = frozenset({
    "signal_decisions", "signal_shadow", "ohlcv_slice_sets", "ohlcv_slices",
    "ohlcv_slice_generations", "econ_reaction_keys", "econ_reaction_values"})

REPORT_DIR_NAME = "econ_signal"

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


# --- worker の生死 ---------------------------------------------------------

class WorkerHealth:
    """slice worker の thread を status から観測するための入れ物。

    thread を作る側 (`run_service`) が `attach` / `start_failed` を呼ぶ。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._start_failed = False

    def attach(self, thread: threading.Thread | None) -> None:
        with self._lock:
            self._thread = thread

    def mark_start_failed(self) -> None:
        with self._lock:
            self._start_failed = True

    def state(self) -> str:
        """`alive` / `stopped` (起動した thread が終わった) / `start_failed` / `not_started`。"""
        with self._lock:
            thread, failed = self._thread, self._start_failed
        if failed:
            return "start_failed"
        if thread is None:
            return "not_started"
        return "alive" if thread.is_alive() else "stopped"


# --- 容量 --------------------------------------------------------------------

def slice_related_bytes(conn: sqlite3.Connection) -> int:
    """slice 関連 7 表とその index の bytes (`dbstat` の集計)。"""
    owner = {name: table for name, table in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
    total = 0
    for name, size in conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
        if owner.get(name) in SLICE_RELATED_TABLES:
            total += size or 0
    return total


_EMPTY_BASELINE: list[int] = []


def empty_slice_bytes() -> int:
    """空の DDL だけの DB での slice 関連 bytes。実行時の増分の起点。"""
    if not _EMPTY_BASELINE:
        from agentic_fx.store.db import init_db
        probe = sqlite3.connect(":memory:")
        probe.row_factory = sqlite3.Row
        try:
            init_db(probe)
            _EMPTY_BASELINE.append(slice_related_bytes(probe))
        finally:
            probe.close()
    return _EMPTY_BASELINE[0]


def _file_bytes(path: Path) -> dict:
    out = {}
    for key, suffix in (("db_bytes", ""), ("wal_bytes", "-wal")):
        try:
            out[key] = os.stat(f"{path}{suffix}").st_size
        except OSError:
            out[key] = 0 if suffix else None
    return out


def _free_bytes(path: Path) -> int | None:
    try:
        stat = os.statvfs(path.parent)
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize


def capacity_status(plan, *, rows: int, slice_bytes: int | None, db_path: Path | None,
                    free_bytes: int | None = None, base_bytes: int | None = None) -> dict:
    """容量の表示項目。`plan` は `ohlcv_slices.CapacityPlan`。

    byte の比較値は B0 に、空 DDL からの slice 関連 bytes の増分だけを足したもの。"""
    level_rows = ohlcv_slices.capacity_level(rows, plan.rows)
    used_bytes = None
    level_bytes = None
    if slice_bytes is not None:
        baseline = empty_slice_bytes() if base_bytes is None else base_bytes
        used_bytes = ohlcv_slices.SLICE_BASE_BYTES + max(0, slice_bytes - baseline)
        level_bytes = ohlcv_slices.capacity_level(used_bytes, plan.bytes)
    level = max((level_rows, level_bytes or "ok"),
                key=("ok", "warn", "full").index)
    files = _file_bytes(db_path) if db_path is not None else {}
    return {
        "signal_daily": plan.signals.value,
        "signal_producer": plan.signals.producer,
        "signal_override": plan.signals.override,
        "capacity_override_below_producer": plan.signals.below_producer,
        "rows": rows, "row_capacity": plan.rows, "row_level": level_rows,
        "slice_bytes": slice_bytes, "used_bytes": used_bytes,
        "byte_capacity": plan.bytes, "byte_level": level_bytes,
        "base_bytes": ohlcv_slices.SLICE_BASE_BYTES,
        "bytes_per_row": ohlcv_slices.SLICE_BYTES_PER_ROW,
        "level": level,
        "free_bytes": free_bytes if free_bytes is not None
        else (_free_bytes(db_path) if db_path is not None else None),
        **files,
    }


class CapacityReader:
    """容量の表示値の読み手。行数と slice 関連 bytes は表の全域を数えるので、
    `ttl_sec` の間は直前の値を返す (status の呼出しごとに全走査しない)。"""

    def __init__(self, plan_fn: Callable[[], object], db_path: Path, *,
                 ttl_sec: float = 300.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._plan_fn = plan_fn
        self._db_path = Path(db_path)
        self._ttl = ttl_sec
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: tuple[float, int, int | None] | None = None

    def _measure(self, conn: sqlite3.Connection) -> tuple[int, int | None]:
        with self._lock:
            now = self._monotonic()
            if self._cached is not None and now - self._cached[0] < self._ttl:
                return self._cached[1], self._cached[2]
        rows = ohlcv_slices.slice_row_count(conn)
        try:
            size: int | None = slice_related_bytes(conn)
        except sqlite3.Error as exc:
            _log.warning("slice bytes unavailable: %s", safe_error_text(exc))
            size = None
        with self._lock:
            self._cached = (self._monotonic(), rows, size)
        return rows, size

    def status(self, conn: sqlite3.Connection) -> dict:
        plan = self._plan_fn()
        rows, size = self._measure(conn)
        return capacity_status(plan, rows=rows, slice_bytes=size,
                               db_path=self._db_path)


# --- abandoned と mission の終端 -------------------------------------------

def abandoned_status(conn: sqlite3.Connection, *, now: datetime) -> dict:
    """abandoned を `abandoned_at` で帰属させた理由別件数。

    `total` は累計 (時刻の無い `legacy_unknown` を除く)、`latest_day` は直近運用日。
    `cleanup` と `legacy_unknown` は別枠で、decision の母数には合算しない。"""
    day_start = market_hours.trading_day_start(now)
    until = now + timedelta(minutes=1)
    total = signals.human_signal_aggregates(conn, since=_EPOCH, until=until)
    latest = signals.human_signal_aggregates(conn, since=day_start, until=until)

    def by_reason(counts: dict) -> dict:
        return {reason: counts.get(reason, 0) for reason in ABANDON_REASONS}

    return {
        "total": by_reason(total["abandoned"]),
        "latest_day": by_reason(latest["abandoned"]),
        "latest_day_start": day_start.isoformat(),
        "cleanup": {"total": total["cleanup"], "latest_day": latest["cleanup"]},
        "legacy_unknown_total": total["legacy_unknown_total"],
    }


def decision_status(conn: sqlite3.Connection, *, since: datetime,
                    until: datetime) -> dict:
    """decision 行だけを母数にした accept / reject の件数。abandoned は入れない。"""
    agg = signals.human_signal_aggregates(conn, since=since, until=until)
    counts: dict[str, int] = {}
    for row in agg["decisions"]:
        key = row["decision"] if row["decision"] == "accept" else (
            f"reject:{row['actor']}:{row['reject_code']}")
        counts[key] = counts.get(key, 0) + row["count"]
    return {"total": sum(counts.values()), "by_kind": counts}


def mission_status_counts(conn: sqlite3.Connection, *, since: datetime,
                          until: datetime) -> dict:
    """期間に始まった mission の status 別件数。`system_filtered` は既知の終端で、
    failed にも running にも数えない。未知の status は `other` にまとめる。"""
    out = {status: 0 for status in (*KNOWN_MISSION_TERMINALS, "running")}
    out["other"] = 0
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM missions WHERE datetime(started_at) >= "
        "datetime(?) AND datetime(started_at) < datetime(?) GROUP BY status",
        (since.isoformat(), until.isoformat())).fetchall()
    for status, count in rows:
        out[status if status in out else "other"] += count
    return out


def shadow_status(conn: sqlite3.Connection) -> list[dict]:
    """shadow を decision 種別 × 状態 × 結果に分けた件数と pnl_pips の平均。"""
    rows = conn.execute(
        "SELECT decision, actor, reject_code, state, result_code, COUNT(*) AS n, "
        "AVG(pnl_pips) AS avg_pnl_pips FROM signal_shadow "
        "GROUP BY decision, actor, reject_code, state, result_code "
        "ORDER BY decision, actor, reject_code, state, result_code").fetchall()
    return [dict(zip(("decision", "actor", "reject_code", "state", "result_code",
                      "n", "avg_pnl_pips"), tuple(row))) for row in rows]


def econ_signal_status(conn: sqlite3.Connection, *, now: datetime,
                       capacity: CapacityReader | None,
                       worker: WorkerHealth | None) -> dict:
    """`afx ctl status` に足す項目。節ごとに失敗を隔離し、取れない節は None にする。"""
    out: dict = {}

    def section(name: str, fn):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001
            _log.warning("econ signal status %s unavailable: %s", name,
                         safe_error_text(exc))
            out[name] = None

    day_start = market_hours.trading_day_start(now)
    until = now + timedelta(minutes=1)
    section("abandoned", lambda: abandoned_status(conn, now=now))
    section("decisions", lambda: decision_status(conn, since=_EPOCH, until=until))
    section("missions_latest_day",
            lambda: mission_status_counts(conn, since=day_start, until=until))
    section("slice_capacity",
            lambda: None if capacity is None else capacity.status(conn))
    out["slice_worker"] = "unknown" if worker is None else worker.state()
    return out


def display_mission_status(status: object) -> str:
    """mission の status の表示。`system_filtered` は帯内で見送った既知の終端で、
    失敗や実行中ではないことを添える。"""
    if status == "system_filtered":
        return "system_filtered (filtered, not failed)"
    return str(status)


def format_econ_lines(econ: dict | None) -> list[str]:
    """`econ_signal_status` の値を人が読む行にする。取れない節は unavailable。"""
    if not isinstance(econ, dict):
        return []
    lines = ["econ signal:" + (f" worker={econ['slice_worker']}"
                              if "slice_worker" in econ else "")]
    dec = econ.get("decisions")
    lines.append("  decisions: " + ("unavailable" if dec is None else
                 f"{dec['total']} " + ", ".join(
                     f"{k}={v}" for k, v in sorted(dec["by_kind"].items()))).rstrip())
    ab = econ.get("abandoned")
    if ab is None:
        lines.append("  abandoned: unavailable")
    else:
        parts = [f"{r}={ab['latest_day'][r]}/{ab['total'][r]}"
                 for r in ABANDON_REASONS if ab["total"][r] or ab["latest_day"][r]]
        lines.append("  abandoned (latest day/total): "
                     + (", ".join(parts) if parts else "none"))
        lines.append(f"  cleanup={ab['cleanup']['latest_day']}/{ab['cleanup']['total']} "
                     f"legacy_unknown={ab['legacy_unknown_total']}")
    miss = econ.get("missions_latest_day")
    if miss is not None:
        lines.append(f"  missions (latest day): system_filtered={miss['system_filtered']} "
                     f"failed={miss['failed']} running={miss['running']}")
    cap = econ.get("slice_capacity")
    if "slice_capacity" not in econ:
        pass
    elif cap is None:
        lines.append("  slice capacity: unavailable")
    else:
        lines.append(
            f"  slice capacity: rows {cap['rows']}/{cap['row_capacity']} "
            f"({cap['row_level']}) bytes {_fmt(cap['used_bytes'])}/{cap['byte_capacity']} "
            f"({_fmt(cap['byte_level'])}) S={cap['signal_daily']} "
            f"B0={cap['base_bytes']} beta={cap['bytes_per_row']} db={_fmt(cap['db_bytes'])} "
            f"wal={_fmt(cap['wal_bytes'])} free={_fmt(cap['free_bytes'])}")
        if cap["capacity_override_below_producer"]:
            lines.append("  warning: capacity_override_below_producer")
        if cap["level"] != "ok":
            lines.append(f"  warning: slice capacity {cap['level']}")
    return lines


# --- 日次 report -----------------------------------------------------------

def _fmt(value) -> str:
    return "-" if value is None else str(value)


def report_label(close_at: datetime) -> date:
    """閉じた境界の 1 分前の取引日ラベル。"""
    return market_hours.trading_date(close_at - timedelta(minutes=1))


def render_report(facts: dict, *, label: date, close_at: datetime) -> str:
    lines = [f"# econ signal daily report {label.isoformat()}", "",
             f"close_at: {close_at.isoformat()}", ""]
    ab = facts.get("abandoned")
    lines.append("## abandoned (reason, abandoned_at 基準)")
    if ab is None:
        lines.append("unavailable")
    else:
        lines.append(f"latest_day_start: {ab['latest_day_start']}")
        for reason in ABANDON_REASONS:
            lines.append(f"- {reason}: day={ab['latest_day'][reason]} "
                         f"total={ab['total'][reason]}")
        lines.append(f"- ineligible_cleanup: day={ab['cleanup']['latest_day']} "
                     f"total={ab['cleanup']['total']}")
        lines.append(f"- legacy_unknown (累計のみ): {ab['legacy_unknown_total']}")
    lines += ["", "## decisions (decision 行のみ)"]
    dec = facts.get("decisions")
    if dec is None:
        lines.append("unavailable")
    else:
        lines.append(f"total: {dec['total']}")
        lines += [f"- {k}: {v}" for k, v in sorted(dec["by_kind"].items())]
    lines += ["", "## missions (直近運用日)"]
    miss = facts.get("missions_latest_day")
    lines += (["unavailable"] if miss is None
              else [f"- {k}: {v}" for k, v in miss.items()])
    lines += ["", "## shadow"]
    shadow = facts.get("shadow")
    if shadow is None:
        lines.append("unavailable")
    else:
        for row in shadow:
            lines.append(
                f"- {row['decision']}/{row['actor']}/{_fmt(row['reject_code'])} "
                f"{row['state']}/{_fmt(row['result_code'])}: n={row['n']} "
                f"avg_pnl_pips={_fmt(row['avg_pnl_pips'])}")
    lines += ["", "## slice capacity"]
    cap = facts.get("slice_capacity")
    if cap is None:
        lines.append("unavailable")
    else:
        for key in ("signal_daily", "signal_producer", "signal_override",
                    "capacity_override_below_producer", "rows", "row_capacity",
                    "row_level", "slice_bytes", "used_bytes", "byte_capacity",
                    "byte_level", "base_bytes", "bytes_per_row", "db_bytes",
                    "wal_bytes", "free_bytes", "level"):
            lines.append(f"- {key}: {_fmt(cap.get(key))}")
    lines += ["", "## slice worker", f"- state: {_fmt(facts.get('slice_worker'))}"]
    for key in ("worst_case_guarantee_unavailable", "wait_budget", "window"):
        if key in facts:
            lines += ["", f"## {key}", f"{facts[key]}"]
    return "\n".join(lines) + "\n"


def collect_report_facts(conn: sqlite3.Connection, *, now: datetime,
                         capacity: CapacityReader | None,
                         worker: WorkerHealth | None,
                         admission_status: Callable[[sqlite3.Connection, datetime], dict]
                         | None = None) -> dict:
    facts = econ_signal_status(conn, now=now, capacity=capacity, worker=worker)
    try:
        facts["shadow"] = shadow_status(conn)
    except Exception as exc:  # noqa: BLE001
        _log.warning("econ signal report shadow unavailable: %s", safe_error_text(exc))
        facts["shadow"] = None
    if admission_status is not None:
        try:
            snap = admission_status(conn, now)
            for key in ("worst_case_guarantee_unavailable", "wait_budget", "window"):
                if key in snap:
                    facts[key] = snap[key]
        except Exception as exc:  # noqa: BLE001
            _log.warning("econ signal report admission unavailable: %s",
                         safe_error_text(exc))
    return facts


def write_report_once(reports_dir: Path, *, label: date, body: str) -> Path | None:
    """取引日ごとに 1 file。既にあれば何もせず None。親専有 (dir 0700 / file 0600)。

    part file に書き切ってから `link` で最終名にするので、途中で落ちても最終名の
    欠けた file は残らない (次の試行がやり直せる)。"""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(reports_dir, 0o700)
    final = reports_dir / f"econ-signal-{label.isoformat()}.md"
    if os.path.lexists(final):
        return None
    part = reports_dir / f".econ-signal-{label.isoformat()}.{os.getpid()}.part"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(part, final)
        except FileExistsError:
            return None
    finally:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
    return final


def make_daily_report_callback(*, reports_dir: Path,
                               open_conn: Callable[[], sqlite3.Connection],
                               capacity: CapacityReader | None,
                               worker: WorkerHealth | None,
                               admission_status: Callable | None = None
                               ) -> Callable[[datetime], None]:
    """scheduler の日次 hook (`register_daily_report`) に渡す callback。

    取引日 (`report_label`) で一意なので、at-least-once で呼ばれても 2 つ目は作らない。
    読み取り接続を callback の中で開いて閉じる。例外は呼び出し側 (scheduler) が数える。"""
    def callback(close_at: datetime) -> None:
        label = report_label(close_at)
        if os.path.lexists(Path(reports_dir) / f"econ-signal-{label.isoformat()}.md"):
            return
        conn = open_conn()
        try:
            facts = collect_report_facts(
                conn, now=close_at - timedelta(minutes=1), capacity=capacity, worker=worker,
                admission_status=admission_status)
        finally:
            conn.close()
        write_report_once(reports_dir, label=label,
                          body=render_report(facts, label=label, close_at=close_at))
    return callback

"""指標回避帯・signal 採否・shadow・slice 容量の人間向け集計と、親専有の日次 report。

status と日次 report が同じ集計を使う。agent の prompt・tool・worker へは渡さない。
読む接続は呼び出し側が用意した読み取り接続だけで、書くのは report file だけである。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading
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
    """slice 関連 7 表とその index の bytes (`dbstat` を対象の名前で絞って合計)。

    全 DB を走査せず、対象の表と index の名前ごとに読む。`dbstat` は page 単位の行を
    返すので、呼び出し側の progress handler で途中から打ち切れる (集計 mode では
    1 つの b-tree を 1 回の呼出しで走査し切るため、打ち切れない)。"""
    marks = ",".join("?" * len(SLICE_RELATED_TABLES))
    names = [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index') "
        f"AND tbl_name IN ({marks})", tuple(sorted(SLICE_RELATED_TABLES)))]
    total = 0
    for name in names:
        total += conn.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ?",
            (name,)).fetchone()[0]
    return int(total)


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
                    free_bytes: int | None = None, base_bytes: int | None = None,
                    slice_bytes_measured_at: datetime | None = None) -> dict:
    """容量の表示項目。`plan` は `ohlcv_slices.CapacityPlan`。

    byte の比較値は B0 に、空 DDL からの slice 関連 bytes の増分だけを足したもの。
    `slice_bytes` が None は未測定 (測る側がまだ測っていない)。"""
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
        "slice_bytes_measured_at": (None if slice_bytes_measured_at is None
                                    else slice_bytes_measured_at.isoformat()),
        "byte_capacity": plan.bytes, "byte_level": level_bytes,
        "base_bytes": ohlcv_slices.SLICE_BASE_BYTES,
        "bytes_per_row": ohlcv_slices.SLICE_BYTES_PER_ROW,
        "level": level,
        "free_bytes": free_bytes if free_bytes is not None
        else (_free_bytes(db_path) if db_path is not None else None),
        **files,
    }


def stored_slice_rows(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """purge していない set が持つ slice 行数。

    保持期限内の complete は `expected_rows` (set の表は小さく、slice 行の全走査を
    しない)。保持期限を過ぎて purge 中の complete / unrecoverable と、complete でない
    set (staging など) は今ある行数。1 つの SELECT で読むので同じ snapshot になる。"""
    cutoff = signals.utc_iso((now or datetime.now(timezone.utc))
                             - timedelta(days=ohlcv_slices.RETENTION_DAYS))
    row = conn.execute(
        "SELECT "
        "(SELECT COALESCE(SUM(expected_rows), 0) FROM ohlcv_slice_sets "
        " WHERE purged_at IS NULL AND state = 'complete' AND created_at >= :cutoff) "
        "+ (SELECT COUNT(*) FROM ohlcv_slices WHERE set_id IN "
        "   (SELECT id FROM ohlcv_slice_sets WHERE purged_at IS NULL "
        "    AND (state != 'complete' OR created_at < :cutoff)))",
        {"cutoff": cutoff}).fetchone()
    return int(row[0])


class CapacityReader:
    """容量の表示値の読み手。行数は set の表から出す。slice 関連 bytes は表の全域を
    読む重い集計なので status の要求では測らず、`bytes_source` (slice worker が定期に
    測って保持した値と時刻) を読むだけにする。"""

    def __init__(self, plan_fn: Callable[[], object], db_path: Path, *,
                 bytes_source: Callable[[], tuple[object, datetime] | None]
                 | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock
        self._plan_fn = plan_fn
        self._db_path = Path(db_path)
        self._bytes_source = bytes_source

    def attach_bytes_source(
            self, source: Callable[[], tuple[object, datetime] | None]) -> None:
        self._bytes_source = source

    def _measured(self) -> tuple[int | None, datetime | None]:
        measured = None if self._bytes_source is None else self._bytes_source()
        if measured is None:
            return None, None
        value, at = measured
        if type(value) is not int:
            return None, None
        return value, at

    def status(self, conn: sqlite3.Connection) -> dict:
        plan = self._plan_fn()
        size, at = self._measured()
        return capacity_status(plan, rows=stored_slice_rows(
            conn, now=None if self._clock is None else self._clock()),
                               slice_bytes=size, db_path=self._db_path,
                               slice_bytes_measured_at=at)


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
    section("shadow", lambda: shadow_status(conn))
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
                 f"{_fmt(dec['total'])} " + ", ".join(
                     f"{k}={v}" for k, v in sorted(dec["by_kind"].items()))).rstrip())
    ab = econ.get("abandoned")
    if ab is None:
        lines.append("  abandoned: unavailable")
    else:
        day, total = ab.get("latest_day") or {}, ab.get("total") or {}
        parts = [f"{r}={_fmt(day.get(r))}/{_fmt(total.get(r))}"
                 for r in ABANDON_REASONS if total.get(r) or day.get(r)]
        lines.append("  abandoned (latest day/total): "
                     + (", ".join(parts) if parts else "none"))
        cleanup = ab.get("cleanup") or {}
        lines.append(f"  cleanup={_fmt(cleanup.get('latest_day'))}/{_fmt(cleanup.get('total'))} "
                     f"legacy_unknown={_fmt(ab.get('legacy_unknown_total'))}")
    miss = econ.get("missions_latest_day")
    if miss is not None:
        lines.append(f"  missions (latest day): "
                     f"system_filtered={_fmt(miss.get('system_filtered'))} "
                     f"failed={_fmt(miss.get('failed'))} running={_fmt(miss.get('running'))}")
    shadow = econ.get("shadow")
    if shadow is not None:
        lines.append("  shadow: " + (", ".join(
            f"{_shadow_label(row)}={row['n']}" for row in shadow) or "none"))
    cap = econ.get("slice_capacity")
    if "slice_capacity" not in econ:
        pass
    elif cap is None:
        lines.append("  slice capacity: unavailable")
    else:
        unmeasured = cap.get("slice_bytes") is None
        lines.append(
            f"  slice capacity: rows {cap['rows']}/{cap['row_capacity']} "
            f"({cap['row_level']}) bytes "
            + ("未測定" if unmeasured else
               f"{_fmt(cap['used_bytes'])}/{cap['byte_capacity']} ({_fmt(cap['byte_level'])})")
            + f" S={cap['signal_daily']} (producer={_fmt(cap.get('signal_producer'))} "
            f"override={_fmt(cap.get('signal_override'))}) "
            f"B0={cap['base_bytes']} beta={cap['bytes_per_row']} db={_fmt(cap['db_bytes'])} "
            f"wal={_fmt(cap['wal_bytes'])} free={_fmt(cap['free_bytes'])}")
        if not unmeasured:
            lines.append(f"  slice bytes: {cap['slice_bytes']} "
                         f"(measured_at={_fmt(cap.get('slice_bytes_measured_at'))})")
        if cap["capacity_override_below_producer"]:
            lines.append("  warning: capacity_override_below_producer")
        if cap["level"] != "ok":
            lines.append(f"  warning: slice capacity {cap['level']}")
    return lines


# --- 日次 report -----------------------------------------------------------

def _shadow_label(row: dict) -> str:
    return (f"{row['decision']}/{row['actor']}/{_fmt(row['reject_code'])}"
            f":{row['state']}/{_fmt(row['result_code'])}")


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
                    "row_level", "slice_bytes", "slice_bytes_measured_at", "used_bytes", "byte_capacity",
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


class ReportStoreError(OSError):
    """report の保存先・最終名が親専有の通常形でない (symlink・dir・他 owner・mode 違い)。"""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _report_name(label: date) -> str:
    return f"econ-signal-{label.isoformat()}.md"


def _open_reports_dir(reports_dir: Path, *, create: bool) -> int:
    """保存 dir を symlink を辿らずに開く。作るときも末尾を辿らない。"""
    reports_dir = Path(reports_dir)
    if create:
        reports_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_fd = os.open(reports_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                os.mkdir(reports_dir.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            return _open_nofollow(reports_dir.name, parent_fd)
        finally:
            os.close(parent_fd)
    return _open_nofollow(str(reports_dir), None)


def _open_nofollow(name: str, dir_fd: int | None) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ReportStoreError(f"report directory is not a plain directory: {exc}") from exc


def _final_is_generated(dir_fd: int, name: str) -> bool:
    """最終名が無ければ False、親専有の通常 file なら True、それ以外は失敗。"""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
            and stat.S_IMODE(st.st_mode) == 0o600):
        raise ReportStoreError("existing report is not an owner-only regular file")
    return True


def report_exists(reports_dir: Path, *, label: date) -> bool:
    """その取引日の report が生成済みか。保存 dir が無ければ False、形が不正なら例外。"""
    try:
        dir_fd = _open_reports_dir(reports_dir, create=False)
    except FileNotFoundError:
        return False
    try:
        return _final_is_generated(dir_fd, _report_name(label))
    finally:
        os.close(dir_fd)


def write_report_once(reports_dir: Path, *, label: date, body: str) -> Path | None:
    """取引日ごとに 1 file。既にあれば何もせず None。親専有 (dir 0700 / file 0600)。

    保存 dir は symlink を辿らずに開き、以降は dir fd 相対で作る。part file に書き切って
    から `link` で最終名にするので、途中で落ちても最終名の欠けた file は残らない
    (次の試行がやり直せる)。最終名が通常 file でない・owner が違う・mode が違うときは
    生成済みとみなさず `ReportStoreError` を上げる。"""
    reports_dir = Path(reports_dir)
    dir_fd = _open_reports_dir(reports_dir, create=True)
    try:
        os.fchmod(dir_fd, 0o700)
        name = _report_name(label)
        if _final_is_generated(dir_fd, name):
            return None
        part = f".econ-signal-{label.isoformat()}.{os.getpid()}.part"
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dir_fd)
        try:
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, body.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(part, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except FileExistsError:
                _final_is_generated(dir_fd, name)
                return None
        finally:
            try:
                os.unlink(part, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        return reports_dir / name
    finally:
        os.close(dir_fd)


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
        if report_exists(reports_dir, label=label):
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

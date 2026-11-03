"""One-off repair for the DST-mislabelled MT5 history.

History imports are immutable by contract.  This module is the sole exception:
it changes only the fingerprinted rows after a dry-run plan has been explicitly
confirmed, and records an idempotency marker in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPAIR_ID = "time_repair_dst_v1"


class RepairError(RuntimeError):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class HistoryFingerprint:
    start: str
    end: str
    counts: dict[str, int]
    key_sha256: str
    value_sha256: str


@dataclass(frozen=True)
class CacheSet:
    name: str
    interval: str
    start: str
    end: str
    count: int
    key_sha256: str
    value_sha256: str


HISTORY_FINGERPRINT = HistoryFingerprint(
    "2025-11-02T21:00:00+00:00", "2026-03-06T20:55:00+00:00",
    {"EURUSD": 25344, "USDJPY": 25344, "JP225": 23906, "US500": 23923,
     "USOIL": 23987, "XAUUSD": 24128},
    "08ce136a3d8c2f98ccae9e039b2a9566559f3a0c5d414b5c57c5b4292ee6fddf",
    "9df5d76f50417d0672c61b7c730d88a2c29657ba7ffa345180f3293c2d8f242b")
CACHE_SETS: tuple[CacheSet, ...] = (
    CacheSet("C1-1m", "1m", "2026-10-02T21:00:00+00:00", "2026-10-03T11:59:00+00:00", 900,
             "6255e3e0eb0d34fb5b3479759d26e1f725440ab31afd0c29740579222ccfd132",
             "780b963266c7fd049a7c2b30b64eb5555ba7fc4a12a90fd2119daf8f1a2a7062"),
    CacheSet("C1-15m", "15m", "2026-10-02T21:00:00+00:00", "2026-10-03T11:45:00+00:00", 60,
             "f9cb1db7aadd38ce8ee2d0b89d9307eafc99bc53cc25f74acdd5992382ce5fa6",
             "b42de498fee87089aaa3b5decb8ec46379bbcfd47cd1ae2b9eab6015fdf20565"),
    CacheSet("C1-1h", "1h", "2026-10-02T21:00:00+00:00", "2026-10-03T11:00:00+00:00", 15,
             "d91a2822cc175d30b7f330917f8b2b99ce9b10d239162ce74b40c08b650e425d",
             "7500ba2a673e2c531c81c3882680637d242b77e6710fad7814b754688b4c76c7"),
    CacheSet("C2-1m", "1m", "2026-11-01T21:01:00+00:00", "2026-11-01T21:04:00+00:00", 4,
             "7972b2c141c283c93870ad216ef94fbb92f980ec42871eb3285af993fa3be19e",
             "88aae8a3bd45ac8602c613213c89affc4439c5e323df7f1da2817c3161720c8d"),
    CacheSet("C2-1h", "1h", "2026-11-01T21:00:00+00:00", "2026-11-01T21:00:00+00:00", 1,
             "54b6ea638368521943d3699dd6d3a3395422768c6595528a782848292c692f6a",
             "c7d2bff6916c6adc43615cc0126d0ca4c41e56fa7b64fa056b79603b58d7a274"),
    CacheSet("C3-15m", "15m", "2026-10-30T20:30:00+00:00", "2026-10-30T21:45:00+00:00", 6,
             "6c4f8a938e2b9f61d471c92d42eba91bdedc1e4c451b8a7fd12132683d5ec5d0",
             "f52ae8e514046ce1c1a4f782ab389ba0e2fea6915876bf71ac88307fed914a49"),
)
C1_K_MINUTES = 900
C1_MATCH_COUNTS = {"1m": 633, "15m": 39, "1h": 7}


def _rows_digest(rows, *, values: bool) -> str:
    lines = []
    for row in rows:
        line = "|".join(str(row[name]) for name in ("symbol", "interval", "bar_time", "source"))
        if values:
            line += "|" + "|".join(repr(float(row[name])) for name in
                                      ("open", "high", "low", "close", "volume"))
        lines.append(line)
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def plan_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _history_rows(conn: sqlite3.Connection):
    fp = HISTORY_FINGERPRINT
    rows = conn.execute(
        "SELECT * FROM ohlcv_history WHERE source='mt5' AND bar_time>=? AND bar_time<=? "
        "ORDER BY symbol, interval, bar_time", (fp.start, fp.end)).fetchall()
    wrong_interval = conn.execute(
        "SELECT 1 FROM ohlcv_history WHERE source='mt5' AND bar_time>=? AND bar_time<=? "
        "AND interval!='5m' LIMIT 1", (fp.start, fp.end)).fetchone()
    counts = {r["symbol"]: 0 for r in rows}
    for row in rows:
        counts[row["symbol"]] += 1
    if wrong_interval or counts != fp.counts or _rows_digest(rows, values=False) != fp.key_sha256 \
            or _rows_digest(rows, values=True) != fp.value_sha256:
        raise RepairError("unknown history fingerprint", 3)
    return rows


def _cache_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    answer = []
    for item in CACHE_SETS:
        rows = conn.execute("SELECT * FROM ohlcv_cache WHERE source='mt5-live' AND symbol='USDJPY' "
                            "AND interval=? AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
                            (item.interval, item.start, item.end)).fetchall()
        if not rows:
            oldest = conn.execute("SELECT min(bar_time) FROM ohlcv_cache WHERE source='mt5-live' "
                                  "AND interval=?", (item.interval,)).fetchone()[0]
            if oldest is not None and oldest > item.end:
                answer.append({"name": item.name, "status": "absent_before_cache_window"})
                continue
            raise RepairError(f"cache set {item.name} is unexpectedly absent", 3)
        if len(rows) != item.count or _rows_digest(rows, values=False) != item.key_sha256 \
                or _rows_digest(rows, values=True) != item.value_sha256:
            raise RepairError(f"cache set {item.name} differs", 3)
        answer.append({"name": item.name, "status": "present", "count": len(rows),
                       "key_sha256": _rows_digest(rows, values=False),
                       "value_sha256": _rows_digest(rows, values=True)})
    if not CACHE_SETS:
        raise RepairError("cache fingerprint constants are unavailable", 3)
    closed = conn.execute("SELECT * FROM ohlcv_cache WHERE source='mt5-live'").fetchall()
    from agentic_fx.core.market_hours import is_market_open
    for row in closed:
        instant = datetime.fromisoformat(row["bar_time"])
        if is_market_open(instant):
            continue
        if not any(row["interval"] == item.interval and item.start <= row["bar_time"] <= item.end
                   for item in CACHE_SETS):
            raise RepairError("unexplained closed-market cache row", 3)
    return answer


def _cache_rows(conn: sqlite3.Connection, item: CacheSet):
    return conn.execute("SELECT * FROM ohlcv_cache WHERE source='mt5-live' AND symbol='USDJPY' "
                        "AND interval=? AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
                        (item.interval, item.start, item.end)).fetchall()


def _anchors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    start = datetime(2025, 11, 3, tzinfo=timezone.utc).date()
    for offset in range(18):
        monday = start + timedelta(days=offset * 7)
        sunday = monday - timedelta(days=1)
        end = monday + timedelta(days=4)
        rows = conn.execute("SELECT bar_time, volume FROM ohlcv_history WHERE source='mt5' AND "
                            "symbol='US500' AND interval='5m' AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
                            (f"{monday.isoformat()}T00:00:00+00:00", f"{end.isoformat()}T23:59:00+00:00")).fetchall()
        by_time = {r["bar_time"]: float(r["volume"]) for r in rows}
        cash_hits = []
        wrong_hits = []
        for day in range(5):
            current = monday + timedelta(days=day)
            for hour, hits in ((13, cash_hits), (14, wrong_hits)):
                stamp = f"{current.isoformat()}T{hour:02d}:30:00+00:00"
                volume = by_time.get(stamp)
                prior = [by_time.get((datetime.fromisoformat(stamp) - timedelta(minutes=5 * n)).isoformat())
                         for n in range(1, 7)]
                if volume is not None and all(x is not None for x in prior) and volume >= 2 * (sum(prior) / 6):
                    hits.append(current.isoformat())
        eur = conn.execute("SELECT count(*) FROM ohlcv_history WHERE source='mt5' AND symbol='EURUSD' "
                           "AND interval='5m' AND bar_time>=? AND bar_time<=?",
                           (f"{sunday.isoformat()}T21:00:00+00:00", f"{monday.isoformat()}T13:30:00+00:00")).fetchone()[0]
        ok = len(cash_hits) >= 3 and not wrong_hits and eur == 198
        reasons = []
        if len(cash_hits) < 3:
            reasons.append("us500_1330_below_three_days")
        if wrong_hits:
            reasons.append("us500_1430_has_cash_open_signal")
        if eur != 198:
            reasons.append("eurusd_opening_window_not_contiguous")
        result.append({"week": monday.isoformat(), "us500_1330_days": len(cash_hits),
                       "us500_1430_days": len(wrong_hits), "eurusd_opening_window_bars": eur,
                       "passed": ok, "reasons": reasons})
    return result


def _row_week(stamp: str) -> str:
    day = datetime.fromisoformat(stamp).date()
    monday = day + timedelta(days=1) if day.weekday() == 6 else day - timedelta(days=day.weekday())
    return monday.isoformat()


_BRIDGE_WINDOWS = (
    ("2025-11-02T22:00:00+00:00", "2025-11-03T00:30:00+00:00", 3600),
    ("2026-01-09T12:00:00+00:00", "2026-01-09T15:00:00+00:00", 3600),
    ("2026-03-06T20:00:00+00:00", "2026-03-06T22:00:00+00:00", 3600),
    ("2026-07-02T12:00:00+00:00", "2026-07-02T14:00:00+00:00", 0),
)


def _bridge_check(conn: sqlite3.Connection, bridge_url: str, fetch=None) -> dict[str, Any]:
    import httpx
    def get(path: str, params=None):
        if fetch is not None:
            return fetch(path, params or {})
        response = httpx.get(bridge_url.rstrip("/") + path, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    def server_time():
        try:
            payload = get("/server-time")
        except Exception as exc:
            raise RepairError(f"bridge server-time unavailable: {exc}", 1) from exc
        age = payload.get("measured_age_sec")
        if (payload.get("offset_source") != "rule" or payload.get("time_rule_state") != "ok"
                or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0 or age > 300):
            raise RepairError("bridge time rule is not freshly verified", 1)
        return payload
    before = server_time()
    windows = []
    names = ("EURUSD", "USDJPY", "JP225", "US500", "USOIL", "XAUUSD")
    for start, end, shift in _BRIDGE_WINDOWS:
        actual_start = datetime.fromisoformat(start)
        actual_end = datetime.fromisoformat(end)
        for symbol in names:
            matched = 0
            bars = 0
            try:
                payload = get(f"/ohlcv/{symbol}", {"from": start, "to": end, "interval": "5m"})
            except Exception as exc:
                raise RepairError(f"bridge OHLCV request failed: {exc}", 1) from exc
            remote = payload.get("bars", [])
            for bar in remote:
                stamp = datetime.fromisoformat(bar["time"])
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise RepairError("bridge returned a naive bar time", 3)
                stamp = stamp.astimezone(timezone.utc)
                hist_stamp = (stamp - timedelta(seconds=shift)).isoformat()
                local = conn.execute("SELECT open,high,low,close,volume FROM ohlcv_history WHERE source='mt5' "
                                     "AND symbol=? AND interval='5m' AND bar_time=?", (symbol, hist_stamp)).fetchone()
                bars += 1
                if local is not None and all(float(local[i]) == float(bar[k]) for i, k in
                                             enumerate(("open", "high", "low", "close", "volume"))):
                    matched += 1
            if shift == 0:
                expected = conn.execute("SELECT count(*) FROM ohlcv_history WHERE source='mt5' AND symbol=? "
                                        "AND interval='5m' AND bar_time>=? AND bar_time<?",
                                        (symbol, start, end)).fetchone()[0]
            else:
                expected = conn.execute("SELECT count(*) FROM ohlcv_history WHERE source='mt5' AND symbol=? "
                                        "AND interval='5m' AND bar_time>=? AND bar_time<?",
                                        (symbol, (actual_start - timedelta(seconds=shift)).isoformat(),
                                         (actual_end - timedelta(seconds=shift)).isoformat())).fetchone()[0]
            remote_count = sum(1 for bar in remote)
            if remote_count != expected:
                raise RepairError(f"bridge window count differs for {symbol} {start}", 3)
            if matched != bars:
                raise RepairError(f"bridge window OHLC differs: {symbol} {start}", 3)
            windows.append({"symbol": symbol, "from": start, "to": end,
                            "count": bars, "matched": matched})
    after = server_time()
    return {"before": before, "after": after, "windows": windows,
            "count": len(windows), "matched": sum(x["matched"] for x in windows)}


def _cache_changes(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[tuple[Any, ...]]]:
    """Validate the pinned cache rows and return their explicit mutation list."""
    changes: list[dict[str, Any]] = []
    replacements: list[tuple[Any, ...]] = []
    from agentic_fx.core.market_hours import is_market_open
    for item in CACHE_SETS:
        rows = _cache_rows(conn, item)
        if not rows:
            changes.append({"name": item.name, "status": "absent_before_cache_window",
                            "delete": 0, "replace": 0})
            continue
        if len(rows) != item.count or _rows_digest(rows, values=False) != item.key_sha256 \
                or _rows_digest(rows, values=True) != item.value_sha256:
            raise RepairError(f"cache set {item.name} differs", 3)
        if item.name.startswith("C1-"):
            minutes = {"1m": 1, "15m": 15, "1h": 60}[item.interval]
            instants = [datetime.fromisoformat(row["bar_time"]) for row in rows]
            if any(is_market_open(t) for t in instants) or any(
                    (b - a).total_seconds() != minutes * 60 for a, b in zip(instants, instants[1:])):
                raise RepairError(f"cache set {item.name} is not a closed continuous run", 3)
            matches = 0
            for row in rows:
                prior = (datetime.fromisoformat(row["bar_time"]) - timedelta(minutes=C1_K_MINUTES)).isoformat()
                match = conn.execute("SELECT open,high,low,close,volume FROM ohlcv_cache "
                                     "WHERE source='mt5-live' AND symbol='USDJPY' AND interval=? AND bar_time=?",
                                     (item.interval, prior)).fetchone()
                if match and tuple(match) == tuple(row[k] for k in ("open", "high", "low", "close", "volume")):
                    matches += 1
            if matches != C1_MATCH_COUNTS[item.interval]:
                raise RepairError(f"cache set {item.name} offset matches differ", 3)
            changes.append({"name": item.name, "status": "present", "delete": len(rows), "replace": 0,
                            "key_sha256": item.key_sha256, "value_sha256": item.value_sha256})
        elif item.name.startswith("C2-"):
            for row in rows:
                if is_market_open(datetime.fromisoformat(row["bar_time"])):
                    raise RepairError(f"cache set {item.name} includes open-market row", 3)
                following = (datetime.fromisoformat(row["bar_time"]) + timedelta(hours=1)).isoformat()
                opened = conn.execute("SELECT 1 FROM ohlcv_cache WHERE source='mt5-live' AND symbol=? "
                                      "AND interval=? AND bar_time=?", (row["symbol"], row["interval"], following)).fetchone()
                if not opened or not is_market_open(datetime.fromisoformat(following)):
                    raise RepairError(f"cache set {item.name} offset evidence is missing", 3)
            changes.append({"name": item.name, "status": "present", "delete": len(rows), "replace": 0,
                            "key_sha256": item.key_sha256, "value_sha256": item.value_sha256})
        else:
            replace_count = 0
            for row in rows:
                stamp = datetime.fromisoformat(row["bar_time"])
                prior = (stamp - timedelta(hours=1)).isoformat()
                old_minutes = conn.execute("SELECT open,high,low,close,volume FROM ohlcv_cache "
                                           "WHERE source='mt5-live' AND symbol='USDJPY' AND interval='1m' "
                                           "AND bar_time>=? AND bar_time<? ORDER BY bar_time",
                                           (prior, (datetime.fromisoformat(prior) + timedelta(minutes=15)).isoformat())).fetchall()
                if len(old_minutes) != 15:
                    raise RepairError("C3 delayed 1m evidence is missing", 3)
                expected_old = (old_minutes[0][0], max(x[1] for x in old_minutes), min(x[2] for x in old_minutes),
                                old_minutes[-1][3], sum(x[4] for x in old_minutes))
                if tuple(row[k] for k in ("open", "high", "low", "close", "volume")) != expected_old:
                    raise RepairError("C3 delayed aggregate differs", 3)
                if (stamp.hour, stamp.minute) in {(20, 30), (20, 45)}:
                    minutes = conn.execute("SELECT open,high,low,close,volume FROM ohlcv_cache "
                                           "WHERE source='mt5-live' AND symbol='USDJPY' AND interval='1m' "
                                           "AND bar_time>=? AND bar_time<? ORDER BY bar_time",
                                           (stamp.isoformat(), (stamp + timedelta(minutes=15)).isoformat())).fetchall()
                    if len(minutes) != 15:
                        raise RepairError("C3 replacement 1m rows are incomplete", 3)
                    agg = (minutes[0][0], max(x[1] for x in minutes), min(x[2] for x in minutes),
                           minutes[-1][3], sum(x[4] for x in minutes))
                    if agg == tuple(row[k] for k in ("open", "high", "low", "close", "volume")):
                        raise RepairError("C3 row is not stale relative to same-label 1m bars", 3)
                    replacements.append((row["symbol"], row["interval"], row["bar_time"], *agg, row["source"]))
                    replace_count += 1
                else:
                    same_minutes = conn.execute("SELECT open,high,low,close,volume FROM ohlcv_cache "
                                                "WHERE source='mt5-live' AND symbol='USDJPY' AND interval='1m' "
                                                "AND bar_time>=? AND bar_time<? ORDER BY bar_time",
                                                (stamp.isoformat(), (stamp + timedelta(minutes=15)).isoformat())).fetchall()
                    if len(same_minutes) == 15:
                        same_agg = (same_minutes[0][0], max(x[1] for x in same_minutes),
                                    min(x[2] for x in same_minutes), same_minutes[-1][3],
                                    sum(x[4] for x in same_minutes))
                        if same_agg == tuple(row[k] for k in ("open", "high", "low", "close", "volume")):
                            raise RepairError("C3 closed row has valid same-label 1m evidence", 3)
            changes.append({"name": item.name, "status": "present", "delete": len(rows) - replace_count,
                            "replace": replace_count, "key_sha256": item.key_sha256,
                            "value_sha256": item.value_sha256})
    return changes, replacements


def _call_hook(hook, point: str) -> None:
    if hook is not None:
        hook(point)


def _backup_database(conn: sqlite3.Connection, path: Path) -> tuple[str, dict[str, int]]:
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("backup integrity_check failed")
        for name, count in counts.items():
            quoted = '"' + name.replace('"', '""') + '"'
            if dest.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0] != count:
                raise sqlite3.DatabaseError(f"backup row count differs for {name}")
    finally:
        dest.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, counts


def _write_manifest(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmpname, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def _post_state_matches(conn: sqlite3.Connection, plan: dict[str, Any]) -> bool:
    h1_all = conn.execute("SELECT * FROM ohlcv_history WHERE source='mt5' AND interval='5m' "
                      "AND bar_time>? AND bar_time<=? ORDER BY symbol,interval,bar_time",
                      (HISTORY_FINGERPRINT.start, _shift(HISTORY_FINGERPRINT.end))).fetchall()
    excluded = {item["week"] for item in plan.get("excluded_weeks", [])}
    h1 = [row for row in h1_all if _row_week(row["bar_time"]) not in excluded]
    target = plan["targets"]["H1"]
    if not (len(h1) == target["count"] and _rows_digest(h1, values=False) == target["post_key_sha256"]
            and _rows_digest(h1, values=True) == target["post_value_sha256"]):
        return False
    for group in ("C1", "C2", "C3"):
        for item in plan["targets"][group]["sets"]:
            definition = next(x for x in CACHE_SETS if x.name == item["name"])
            rows = _cache_rows(conn, definition)
            if (len(rows) != item["post_count"] or _rows_digest(rows, values=False) != item["post_key_sha256"]
                    or _rows_digest(rows, values=True) != item["post_value_sha256"]):
                return False
    from agentic_fx.core.market_hours import is_market_open
    from agentic_fx.core.fx_time import session_boundary_utc
    if any(not is_market_open(datetime.fromisoformat(r[0])) and _row_week(r[0]) not in excluded for r in
           conn.execute("SELECT bar_time FROM ohlcv_history WHERE source='mt5' AND interval='5m'")):
        return False
    symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM ohlcv_history WHERE source='mt5' AND interval='5m'")]
    bounds = conn.execute("SELECT min(bar_time), max(bar_time) FROM ohlcv_history WHERE source='mt5' AND interval='5m'").fetchone()
    if bounds[0] and bounds[1]:
        first_day = datetime.fromisoformat(bounds[0]).date()
        last_day = datetime.fromisoformat(bounds[1]).date()
        sunday = first_day - timedelta(days=(first_day.weekday() + 1) % 7)
        while sunday <= last_day:
            monday_key = (sunday + timedelta(days=1)).isoformat()
            if monday_key not in excluded:
                opening = session_boundary_utc(sunday)
                friday = sunday + timedelta(days=5)
                closing = session_boundary_utc(friday) - timedelta(minutes=5)
                for symbol in symbols:
                    symbol_bounds = conn.execute("SELECT min(bar_time), max(bar_time) FROM ohlcv_history "
                                                 "WHERE source='mt5' AND symbol=? AND interval='5m'",
                                                 (symbol,)).fetchone()
                    if (symbol_bounds[0] is None or symbol_bounds[0] > opening.isoformat()
                            or symbol_bounds[1] < closing.isoformat()):
                        continue
                    exists = conn.execute("SELECT 1 FROM ohlcv_history WHERE source='mt5' AND symbol=? "
                                          "AND interval='5m' AND bar_time>=? AND bar_time<=? LIMIT 1",
                                          (symbol, opening.isoformat(), closing.isoformat())).fetchone()
                    if exists:
                        has_open = conn.execute("SELECT 1 FROM ohlcv_history WHERE source='mt5' AND symbol=? "
                                                "AND interval='5m' AND bar_time=?", (symbol, opening.isoformat())).fetchone()
                        has_close = conn.execute("SELECT 1 FROM ohlcv_history WHERE source='mt5' AND symbol=? "
                                                 "AND interval='5m' AND bar_time=?", (symbol, closing.isoformat())).fetchone()
                        if not has_open or not has_close:
                            return False
            sunday += timedelta(days=7)
    for day in ("2025-12-16", "2026-03-06", "2026-01-09"):
        if not (bounds[0] <= day + "T13:30:00+00:00" <= bounds[1]):
            continue
        rows = conn.execute("SELECT bar_time, high-low AS range FROM ohlcv_history WHERE source='mt5' "
                            "AND symbol='USDJPY' AND interval='5m' AND bar_time>=? AND bar_time<? "
                            "ORDER BY range DESC", (day + "T00:00:00+00:00", day + "T23:59:59+00:00")).fetchall()
        if not rows:
            return False
        target = next((i for i, row in enumerate(rows) if row["bar_time"] == day + "T13:30:00+00:00"), None)
        if target is None or (day != "2026-01-09" and target != 0) or (day == "2026-01-09" and target > 1):
            return False
    if any(not is_market_open(datetime.fromisoformat(r[0])) for r in
           conn.execute("SELECT bar_time FROM ohlcv_cache WHERE source='mt5-live'")):
        return False
    return conn.execute("SELECT 1 FROM data_migrations WHERE name=?", (REPAIR_ID,)).fetchone() is not None


def build_plan(conn: sqlite3.Connection, *, indicator_pending: str, bridge_url: str | None = None,
               bridge_fetch=None, db_path: Path | None = None) -> dict[str, Any]:
    """Inspect only; no mutation is permitted in this dry-run path."""
    if indicator_pending not in {"keep", "invalidate"}:
        raise RepairError("--indicator-pending is required", 2)
    rows = _history_rows(conn)
    cache_sets = _cache_status(conn)
    cache_changes, cache_replacements = _cache_changes(conn)
    anchors = _anchors(conn)
    bridge_check = _bridge_check(conn, bridge_url, bridge_fetch) if bridge_url else None
    excluded_weeks = [{"week": row["week"], "reason": ",".join(row["reasons"])}
                      for row in anchors if not row["passed"]]
    excluded = {row["week"] for row in excluded_weeks}
    moving_rows = [row for row in rows if _row_week(row["bar_time"]) not in excluded]
    shifted = []
    old_keys = {(row["symbol"], row["interval"], row["bar_time"], row["source"]) for row in moving_rows}
    for row in moving_rows:
        changed = dict(row)
        changed["bar_time"] = _shift(row["bar_time"])
        shifted.append(changed)
        from agentic_fx.core.fx_time import server_epoch_to_utc
        label = datetime.fromisoformat(row["bar_time"])
        if server_epoch_to_utc(int((label + timedelta(hours=3)).timestamp())) != label + timedelta(hours=1):
            raise RepairError("history row does not satisfy the server-time rule", 3)
        collision = conn.execute("SELECT 1 FROM ohlcv_history WHERE symbol=? AND interval=? "
                                 "AND bar_time=? AND source=?", (row["symbol"], row["interval"],
                                 changed["bar_time"], row["source"])).fetchone()
        if collision and (row["symbol"], row["interval"], changed["bar_time"], row["source"]) not in old_keys:
            raise RepairError("history timestamp collision", 3)
    approvals = []
    for row in conn.execute("SELECT id, kind, payload_json FROM approval_requests WHERE status='pending'"):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        is_strategy = payload.get("kind") == "strategy" or payload.get("eval_source") is not None
        is_indicator = payload.get("kind") == "indicator"
        if is_strategy or (indicator_pending == "invalidate" and is_indicator):
            approvals.append({"id": row["id"], "kind": row["kind"], "payload_kind": payload.get("kind"),
                              "status": "invalidated",
                              "payload_sha256": hashlib.sha256(row["payload_json"].encode()).hexdigest()})
    target_groups = {}
    for prefix in ("C1", "C2", "C3"):
        sets = [item for item in cache_changes if item["name"].startswith(prefix)]
        for item in sets:
            definition = next(x for x in CACHE_SETS if x.name == item["name"])
            old = _cache_rows(conn, definition)
            newrows = []
            if prefix == "C3" and old:
                replaced = {r[2]: r for r in cache_replacements}
                for row in old:
                    if row["bar_time"] in replaced:
                        tup = replaced[row["bar_time"]]
                        newrows.append({"symbol": tup[0], "interval": tup[1], "bar_time": tup[2],
                                        "open": tup[3], "high": tup[4], "low": tup[5],
                                        "close": tup[6], "volume": tup[7], "source": tup[8]})
            item["post_count"] = len(newrows)
            item["post_key_sha256"] = _rows_digest(newrows, values=False)
            item["post_value_sha256"] = _rows_digest(newrows, values=True)
        target_groups[prefix] = {"sets": sets,
            "delete": sum(item["delete"] for item in sets),
            "replace": sum(item["replace"] for item in sets)}
    targets = {"H1": {"count": len(rows), "key_sha256": _rows_digest(rows, values=False),
                       "by_symbol": {symbol: sum(1 for row in rows if row["symbol"] == symbol)
                                     for symbol in sorted({row["symbol"] for row in rows})},
                       "value_sha256": _rows_digest(rows, values=True),
                       "post_key_sha256": _rows_digest(shifted, values=False),
                       "post_value_sha256": _rows_digest(shifted, values=True), "shift_seconds": 3600},
               **target_groups}
    db_size = db_path.stat().st_size if db_path and db_path.exists() else 0
    db_free = shutil.disk_usage(db_path.parent).free if db_path else 0
    plan: dict[str, Any] = {"schema": "afx.time_repair.plan/1", "repair_id": REPAIR_ID,
        "created_at": datetime.now(timezone.utc).isoformat(), "fingerprint": {"id": REPAIR_ID,
        "start": HISTORY_FINGERPRINT.start, "end": HISTORY_FINGERPRINT.end,
        "counts": HISTORY_FINGERPRINT.counts,
        "key_sha256": _rows_digest(rows, values=False), "value_sha256": _rows_digest(rows, values=True)},
        "db": {"path": str(db_path.resolve()) if db_path else "", "size_bytes": db_size, "free_bytes": db_free,
               "data_migrations": [r[0] for r in conn.execute("SELECT name FROM data_migrations ORDER BY name")]},
        "cache_sets": cache_sets, "anchors": anchors, "excluded_weeks": excluded_weeks, "bridge_check": bridge_check,
        "targets": targets, "samples": [], "collisions": 0, "unexplained": 0,
        "indicator_pending": indicator_pending, "approval_changes": approvals,
        "estimate_sec": max(1, math.ceil(db_size / (64 * 1024 * 1024)))}
    plan["samples"] = [{"kind": "H1", "before": row["bar_time"], "after": _shift(row["bar_time"]),
                         "symbol": row["symbol"]} for row in rows[:3]]
    for item in CACHE_SETS:
        for row in _cache_rows(conn, item)[:3]:
            after = None
            if item.name.startswith("C3"):
                after = next((list(repl[3:8]) for repl in cache_replacements if repl[2] == row["bar_time"]), None)
            plan["samples"].append({"kind": item.name.split("-")[0], "symbol": row["symbol"],
                                    "before": row["bar_time"], "after": after})
    plan["plan_hash"] = plan_hash(plan)
    return plan


def _shift(iso: str) -> str:
    return (datetime.fromisoformat(iso) + timedelta(hours=1)).isoformat()


def apply_plan(conn: sqlite3.Connection, db_path: Path, plan: dict[str, Any], *, confirm: str,
               root: Path | None = None, hook=None) -> None:
    expected = plan.get("plan_hash")
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    if not isinstance(expected, str) or confirm != expected or plan_hash(payload) != expected:
        raise RepairError("plan confirmation does not match", 4)
    planned_db = plan.get("db", {}).get("path")
    if planned_db and Path(planned_db).resolve() != db_path.resolve():
        raise RepairError("plan names a different database path", 4)
    from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock
    from agentic_fx.plugin.switch import deploy_guard
    root = root or db_path.parent.parent
    try:
        instance_lock = acquire_instance_lock(db_path.parent)
    except InstanceAlreadyRunning as exc:
        raise RepairError(str(exc), 1) from exc
    try:
        with deploy_guard(root / "plugins", exclusive=True):
            marked = conn.execute("SELECT 1 FROM data_migrations WHERE name=?", (REPAIR_ID,)).fetchone()
            if marked:
                if _post_state_matches(conn, plan):
                    return
                raise RepairError("migration marker exists but repaired state differs", 5)
            v0 = conn.execute("PRAGMA data_version").fetchone()[0]
            try:
                current = build_plan(conn, indicator_pending=plan["indicator_pending"])
            except RepairError as exc:
                raise RepairError("database differs from plan", 4) from exc
            if current["targets"] != plan["targets"] or current["approval_changes"] != plan["approval_changes"]:
                raise RepairError("database differs from plan", 4)
            _call_hook(hook, "after_recalculate")
            if shutil.disk_usage(db_path.parent).free < db_path.stat().st_size * 2:
                raise RepairError("insufficient free space for backup", 1)
            backup = db_path.with_name(f"{db_path.name}.bak-time-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            try:
                backup_sha, table_counts = _backup_database(conn, backup)
            except Exception as exc:
                raise RepairError(f"backup failed: {exc}", 1) from exc
            manifest = db_path.with_name(db_path.name + ".time-repair.manifest.json")
            manifest_body = {"path": str(backup.resolve()), "sha256": backup_sha,
                             "plan_hash": expected, "created_at": datetime.now(timezone.utc).isoformat(),
                             "committed": False, "table_counts": table_counts}
            _write_manifest(manifest, manifest_body)
            _call_hook(hook, "after_backup")
            _call_hook(hook, "before_begin")
            conn.execute("BEGIN IMMEDIATE")
            try:
                begin_at = datetime.now(timezone.utc)
                if conn.execute("PRAGMA data_version").fetchone()[0] != v0:
                    raise RepairError("database changed during apply", 4)
                _call_hook(hook, "before_transaction_digest")
                try:
                    before = build_plan(conn, indicator_pending=plan["indicator_pending"])
                except RepairError as exc:
                    raise RepairError("transaction target digest differs", 4) from exc
                if before["targets"] != plan["targets"] or before["approval_changes"] != plan["approval_changes"]:
                    raise RepairError("transaction target digest differs", 4)
                excluded = {item["week"] for item in plan.get("excluded_weeks", [])}
                all_rows = _history_rows(conn)
                rows = [row for row in all_rows if _row_week(row["bar_time"]) not in excluded]
                _, cache_replacements = _cache_changes(conn)
                conn.execute("CREATE TEMP TABLE time_repair_h1 AS SELECT * FROM ohlcv_history WHERE 0")
                for row in all_rows:
                    bar_time = row["bar_time"] if _row_week(row["bar_time"]) in excluded else _shift(row["bar_time"])
                    conn.execute("INSERT INTO time_repair_h1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (row["symbol"], row["interval"], bar_time, row["open"],
                                  row["high"], row["low"], row["close"], row["volume"], row["source"], row["spread"]))
                conn.execute("DELETE FROM ohlcv_history WHERE source='mt5' AND bar_time>=? AND bar_time<=?",
                             (HISTORY_FINGERPRINT.start, HISTORY_FINGERPRINT.end))
                conn.execute("INSERT INTO ohlcv_history SELECT * FROM time_repair_h1")
                conn.execute("DROP TABLE time_repair_h1")
                _call_hook(hook, "after_H1")
                for prefix in ("C1", "C2", "C3"):
                    for item in CACHE_SETS:
                        if not item.name.startswith(prefix):
                            continue
                        state = next(x for x in plan["targets"][prefix]["sets"] if x["name"] == item.name)
                        if state["status"] != "present":
                            continue
                        if prefix == "C3":
                            replacements = [r for r in cache_replacements if r[1] == item.interval]
                            replacement_times = {r[2] for r in replacements}
                            for row in _cache_rows(conn, item):
                                if row["bar_time"] in replacement_times:
                                    newrow = next(r for r in replacements if r[2] == row["bar_time"])
                                    conn.execute("UPDATE ohlcv_cache SET open=?,high=?,low=?,close=?,volume=? "
                                                 "WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
                                                 (*newrow[3:8], row["symbol"], row["interval"], row["bar_time"], row["source"]))
                                else:
                                    conn.execute("DELETE FROM ohlcv_cache WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
                                                 (row["symbol"], row["interval"], row["bar_time"], row["source"]))
                        else:
                            conn.execute("DELETE FROM ohlcv_cache WHERE source='mt5-live' AND symbol='USDJPY' "
                                         "AND interval=? AND bar_time>=? AND bar_time<=?",
                                         (item.interval, item.start, item.end))
                    _call_hook(hook, f"after_{prefix}")
                for change in plan["approval_changes"]:
                    from agentic_fx.store.approvals import apply_decision
                    apply_decision(conn, change["id"], "invalidated", decided_by="system:time_repair",
                                   now=begin_at, reason="history_time_repaired", commit=False)
                _call_hook(hook, "after_approvals")
                conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                             (REPAIR_ID, begin_at.isoformat()))
                _call_hook(hook, "after_marker")
                _call_hook(hook, "before_commit")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            try:
                manifest_body["committed"] = True
                manifest_body["committed_at"] = datetime.now(timezone.utc).isoformat()
                _write_manifest(manifest, manifest_body)
                _call_hook(hook, "after_commit")
                if not _post_state_matches(conn, plan):
                    raise RepairError("post-commit digest or invariants differ", 5)
            except RepairError:
                raise
            except BaseException as exc:
                raise RepairError(f"post-commit verification failed: {exc}", 5) from exc
    finally:
        instance_lock.close()


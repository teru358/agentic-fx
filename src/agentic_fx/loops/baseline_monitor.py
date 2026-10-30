"""親専有の baseline comparison 集計。"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath
from statistics import fmean


def _strict_step(previous: dict, current: dict) -> bool:
    """両指標が同じ向きに厳密に動いた時だけ系列を伸ばす。"""
    try:
        pf0, ar0 = float(previous["pf"]), float(previous["avg_r"])
        pf1, ar1 = float(current["pf"]), float(current["avg_r"])
    except (KeyError, TypeError, ValueError):
        return False
    return (pf1 > pf0 and ar1 > ar0) or (pf1 < pf0 and ar1 < ar0)


def summarize_comparisons(records: list[dict]) -> list[dict]:
    """比較 record を仕様固定の親向け schema に射影する純関数。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if isinstance(record.get("strategy"), str):
            groups[record["strategy"]].append(record)
    output = []
    for strategy in sorted(groups):
        rows = groups[strategy]
        runs: dict[str, tuple[dict, int]] = {}
        hashes = []
        for row in rows:
            base = row.get("base") if isinstance(row.get("base"), str) else ""
            prior = runs.get(base)
            length = prior[1] + 1 if prior is not None and _strict_step(prior[0], row) else 1
            runs[base] = (row, length)
            hashes.append({"candidate": str(row.get("candidate") or "")[:12],
                           "base": base[:12], "verdict": str(row.get("verdict") or ""),
                           "monotone_run": length})
        overlaps = [float(r["window_overlap"]) for r in rows
                    if type(r.get("window_overlap")) in (int, float)]
        attempts = len(rows)
        output.append({"strategy": strategy, "attempts": attempts,
                       "by_candidate_hash": hashes,
                       "not_submitted_share": (sum(bool(r.get("not_submitted")) for r in rows)
                                               / attempts if attempts else 0.0),
                       "unavailable_rate": (sum(r.get("verdict") == "unavailable" for r in rows)
                                            / attempts if attempts else 0.0),
                       "window_overlap": {"mean": fmean(overlaps) if overlaps else None,
                                          "n": len(overlaps)}})
    return output


def monitor_summary(conn: sqlite3.Connection) -> list[dict]:
    """baseline/candidate 行と approval snapshot から親専用 schema を作る。"""
    approval_by_candidate = {}
    approved_windows = {}
    for row in conn.execute("SELECT status,payload_json FROM approval_requests ORDER BY id"):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("content_hash"), str):
            continue
        candidate = payload["content_hash"]
        baseline = payload.get("baseline")
        if isinstance(baseline, dict) and isinstance(baseline.get("comparison"), dict):
            approval_by_candidate[candidate] = (payload, baseline["comparison"])
        if row["status"] == "approved":
            window = _payload_holdout_window(payload)
            if window is not None:
                approved_windows[candidate] = window

    records = []
    rows = conn.execute(
        "SELECT b.plugin_ref,b.content_hash,b.ref_content_hash,b.mission_id,b.mission_outcome,"
        "b.period_start,b.period_end,c.metrics_json FROM backtest_runs b "
        "LEFT JOIN backtest_runs c ON c.mission_id=b.mission_id "
        "AND c.variant='candidate' AND c.scope='holdout_gate' "
        "AND c.pair=b.pair WHERE b.variant='baseline' AND b.scope='holdout_gate' "
        "ORDER BY b.mission_id,b.id").fetchall()
    seen = set()
    for row in rows:
        key = (row["mission_id"], row["ref_content_hash"])
        if key in seen:
            continue
        seen.add(key)
        try:
            metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        except (TypeError, ValueError):
            metrics = {}
        payload, comparison = approval_by_candidate.get(row["ref_content_hash"], ({}, {}))
        verdict = comparison.get("verdict") if isinstance(comparison, dict) else row["mission_outcome"]
        records.append({
            "strategy": _strategy_name(row["plugin_ref"]),
            "candidate": row["ref_content_hash"], "base": row["content_hash"],
            "verdict": verdict, "not_submitted": not bool(payload),
            "pf": metrics.get("pf") if isinstance(metrics, dict) else None,
            "avg_r": metrics.get("avg_r") if isinstance(metrics, dict) else None,
            "window_overlap": _window_overlap(row["period_start"], row["period_end"],
                                                approved_windows.get(row["content_hash"]))})
    return summarize_comparisons(records)


def _strategy_name(plugin_ref: object) -> str | None:
    return PurePosixPath(plugin_ref).name if isinstance(plugin_ref, str) else None


def _payload_holdout_window(payload: dict) -> tuple[datetime, datetime] | None:
    holdout = payload.get("holdout")
    values = holdout.values() if isinstance(holdout, dict) else ()
    for metrics in values:
        if not isinstance(metrics, dict):
            continue
        try:
            return datetime.fromisoformat(metrics["period_start"]), datetime.fromisoformat(metrics["period_end"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _window_overlap(start: object, end: object,
                    adopted: tuple[datetime, datetime] | None) -> float | None:
    if adopted is None or not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        current = (datetime.fromisoformat(start), datetime.fromisoformat(end))
    except ValueError:
        return None
    total = (adopted[1] - adopted[0]).total_seconds()
    overlap = (min(current[1], adopted[1]) - max(current[0], adopted[0])).total_seconds()
    return max(0.0, overlap) / total if total > 0 else None

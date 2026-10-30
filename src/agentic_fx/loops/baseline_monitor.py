"""親専有の baseline comparison 集計。"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath
from statistics import fmean


def _direction(previous: dict, current: dict) -> int:
    """pf と avg_r が両方とも厳密に増えれば +1、両方とも厳密に減れば -1、それ以外は 0。"""
    try:
        pf0, ar0 = float(previous["pf"]), float(previous["avg_r"])
        pf1, ar1 = float(current["pf"]), float(current["avg_r"])
    except (KeyError, TypeError, ValueError):
        return 0
    if pf1 > pf0 and ar1 > ar0:
        return 1
    if pf1 < pf0 and ar1 < ar0:
        return -1
    return 0


def summarize_comparisons(records: list[dict]) -> list[dict]:
    """比較 record を仕様固定の親向け schema に射影する純関数。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if isinstance(record.get("strategy"), str):
            groups[record["strategy"]].append(record)
    output = []
    for strategy in sorted(groups):
        rows = groups[strategy]
        runs: dict[str, tuple[dict, int, int]] = {}
        hashes = []
        for row in rows:
            base = row.get("base") if isinstance(row.get("base"), str) else ""
            prior = runs.get(base)
            step = _direction(prior[0], row) if prior is not None else 0
            if step != 0 and (prior[2] == 0 or prior[2] == step):
                length, heading = prior[1] + 1, step
            else:
                length, heading = 1, 0
            runs[base] = (row, length, heading)
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
    """baseline / candidate 行と approval の比較 snapshot から親専有の schema を作る。"""
    approvals = []
    for row in conn.execute("SELECT id,status,payload_json FROM approval_requests ORDER BY id"):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("content_hash"), str):
            approvals.append((row["id"], row["status"], payload))
    by_candidate = {}
    approved = set()
    for approval_id, status, payload in approvals:
        baseline = payload.get("baseline")
        if isinstance(baseline, dict) and isinstance(baseline.get("comparison"), dict):
            by_candidate[payload["content_hash"]] = (payload, baseline["comparison"])
        if status == "approved":
            approved.add(payload["content_hash"])

    records = []
    seen = set()
    rows = conn.execute(
        "SELECT b.plugin_ref,b.content_hash,b.ref_content_hash,b.mission_id,b.mission_outcome,"
        "b.pair,b.period_start,b.period_end,c.metrics_json FROM backtest_runs b "
        "LEFT JOIN backtest_runs c ON c.mission_id=b.mission_id "
        "AND c.variant='candidate' AND c.scope='holdout_gate' "
        "AND c.content_hash=b.ref_content_hash AND c.pair=b.pair "
        "WHERE b.variant='baseline' AND b.scope='holdout_gate' "
        "ORDER BY b.mission_id,b.id").fetchall()
    keyed = {}
    for row in rows:
        key = (row["mission_id"], row["ref_content_hash"])
        keyed.setdefault(key, []).append(row)
    for key, group in keyed.items():
        first = group[0]
        payload, comparison = by_candidate.get(first["ref_content_hash"], ({}, {}))
        verdict = comparison.get("verdict") if comparison else first["mission_outcome"]
        pf = avg_r = None
        try:
            metrics = json.loads(first["metrics_json"]) if first["metrics_json"] else {}
        except (TypeError, ValueError):
            metrics = {}
        if isinstance(metrics, dict):
            pf, avg_r = metrics.get("pf"), metrics.get("avg_r")
        overlaps = []
        for row in group:
            window = (_adoption_window(conn, row["content_hash"], row["pair"])
                      if row["content_hash"] in approved else None)
            overlap = _window_overlap(row["period_start"], row["period_end"], window)
            if overlap is not None:
                overlaps.append(overlap)
        seen.add(first["ref_content_hash"])
        records.append((first["mission_id"] or 0, {
            "strategy": _strategy_name(first["plugin_ref"]),
            "candidate": first["ref_content_hash"], "base": first["content_hash"],
            "verdict": verdict, "not_submitted": not payload,
            "pf": pf, "avg_r": avg_r,
            "window_overlap": fmean(overlaps) if overlaps else None}))
    for approval_id, _status, payload in approvals:
        candidate = payload["content_hash"]
        baseline = payload.get("baseline")
        comparison = baseline.get("comparison") if isinstance(baseline, dict) else None
        if (candidate in seen or not isinstance(comparison, dict)
                or comparison.get("verdict") != "unavailable"
                or not isinstance(payload.get("name"), str)):
            continue
        seen.add(candidate)
        base = baseline.get("base_content_hash")
        records.append((payload["mission_id"] if type(payload.get("mission_id")) is int
                        else approval_id, {
            "strategy": payload["name"], "candidate": candidate,
            "base": base if isinstance(base, str) else "", "verdict": "unavailable",
            "not_submitted": False, "pf": None, "avg_r": None, "window_overlap": None}))
    records.sort(key=lambda item: item[0])
    return summarize_comparisons([record for _order, record in records])


def _strategy_name(plugin_ref: object) -> str | None:
    return PurePosixPath(plugin_ref).name if isinstance(plugin_ref, str) else None


def _adoption_window(conn: sqlite3.Connection, content_hash: str,
                     pair: str) -> tuple[datetime, datetime] | None:
    """承認済みの版が採用時に通った holdout の期間。記録が無ければ None。"""
    row = conn.execute(
        "SELECT period_start,period_end FROM backtest_runs WHERE variant='candidate' "
        "AND scope='holdout_gate' AND content_hash=? AND pair=? ORDER BY id DESC LIMIT 1",
        (content_hash, pair)).fetchone()
    if row is None:
        return None
    try:
        return (datetime.fromisoformat(row["period_start"]),
                datetime.fromisoformat(row["period_end"]))
    except (TypeError, ValueError):
        return None


def _window_overlap(start: object, end: object,
                    adopted: tuple[datetime, datetime] | None) -> float | None:
    """比較に使った holdout 期間のうち、配備中の版の採用時 holdout 期間と重なる割合。"""
    if adopted is None or not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        current = (datetime.fromisoformat(start), datetime.fromisoformat(end))
        total = (current[1] - current[0]).total_seconds()
        overlap = (min(current[1], adopted[1]) - max(current[0], adopted[0])).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(0.0, overlap) / total if total > 0 else None


MONITOR_SECTION_TITLE = "Baseline monitoring"
_MONITOR_MAX_STRATEGIES = 20
_MONITOR_MAX_HASHES = 20


def render_monitor_section(rows: list[dict]) -> str:
    """親専有レポートの末尾節の本文。この mission 自身は含まない (終端 tx の前に読むため)。"""
    lines = ["Counts cover missions committed before this one.", ""]
    if not rows:
        lines.append("No comparison attempts recorded.")
    for row in rows[:_MONITOR_MAX_STRATEGIES]:
        overlap = row["window_overlap"]
        mean = "null" if overlap["mean"] is None else f"{overlap['mean']:.3f}"
        lines.append(
            f"- {row['strategy']}: attempts={row['attempts']} "
            f"not_submitted_share={row['not_submitted_share']:.3f} "
            f"unavailable_rate={row['unavailable_rate']:.3f} "
            f"window_overlap mean={mean} n={overlap['n']}")
        for item in row["by_candidate_hash"][-_MONITOR_MAX_HASHES:]:
            lines.append(f"  - candidate={item['candidate']} base={item['base']} "
                         f"verdict={item['verdict']} monotone_run={item['monotone_run']}")
    return "\n".join(lines)

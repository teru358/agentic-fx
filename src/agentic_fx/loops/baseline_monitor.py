"""親専有の baseline comparison 集計。"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
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
    """保存済み approval snapshot と candidate 行から比較集計を読む。"""
    records = []
    rows = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE json_extract(payload_json,'$.baseline.comparison') IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            baseline = payload["baseline"]
            comparison = baseline["comparison"]
            holdout = comparison["stages"]["holdout"]
        except (TypeError, ValueError, KeyError):
            continue
        first = next(iter(holdout.values()), None) if isinstance(holdout, dict) else None
        candidate = first.get("candidate") if isinstance(first, dict) else {}
        if not isinstance(candidate, dict):
            candidate = {}
        records.append({"strategy": payload.get("name"), "candidate": payload.get("content_hash"),
                        "base": baseline.get("base_content_hash"),
                        "verdict": comparison.get("verdict"), "not_submitted": False,
                        "pf": candidate.get("pf"), "avg_r": candidate.get("avg_r"),
                        "window_overlap": None})
    return summarize_comparisons(records)

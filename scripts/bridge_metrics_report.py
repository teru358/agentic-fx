#!/usr/bin/env python3
"""`logs/bridge-metrics/` の観測行を集計する (標準ライブラリのみ)。

endpoint x caller ごとに件数・lock の待ち/占有の p50/p95/最大・timeout と失敗の件数・
lock state 別の件数・欠落率を出す。金曜の閉場の 1 時間前から閉場までの行は別の表にする。
read-only。bridge には何も要求しない。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FRIDAY_WINDOW = timedelta(hours=1)


def percentile(values: list[float], pct: float) -> float | None:
    """nearest-rank。空は None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _parse_ts(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _nth_sunday(year: int, month: int, nth: int) -> date:
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def friday_close_utc(day: date) -> datetime | None:
    """金曜の閉場 (NY 17:00)。米国の夏時間は 21:00Z、冬時間は 22:00Z。金曜以外は None。"""
    if day.weekday() != 4:
        return None
    summer = _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)
    return datetime(day.year, day.month, day.day, 21 if summer else 22, 0, tzinfo=timezone.utc)


def friday_close_rows(rows: list[dict]) -> list[dict]:
    picked = []
    for row in rows:
        moment = _parse_ts(row.get("ts"))
        if moment is None:
            continue
        close = friday_close_utc(moment.date())
        if close is not None and close - FRIDAY_WINDOW <= moment <= close:
            picked.append(row)
    return picked


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _stats(values: list[float]) -> dict[str, float | None]:
    return {"p50": percentile(values, 50), "p95": percentile(values, 95),
            "max": max(values) if values else None}


def summarize(rows: list[dict]) -> dict[tuple[str, str], dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("route")), str(row.get("caller")))
        groups.setdefault(key, []).append(row)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in groups.items():
        states = Counter(str(item.get("lock_state")) for item in items)
        waits = [v for v in (_number(i.get("wait_ms")) for i in items) if v is not None]
        holds = [v for v in (_number(i.get("hold_ms")) for i in items) if v is not None]
        timeouts = sum(1 for i in items if i.get("failure") == "timeout")
        failures = sum(1 for i in items if i.get("failure") not in (None, "timeout"))
        http_errors = sum(1 for i in items
                          if isinstance(i.get("status"), int) and i["status"] >= 400)
        lost = states.get("missing", 0) + states.get("unavailable", 0)
        out[key] = {
            "count": len(items), "timeouts": timeouts, "failures": failures,
            "http_errors": http_errors, "lock_states": dict(states),
            "missing_rate": lost / len(items), "wait_ms": _stats(waits), "hold_ms": _stats(holds),
        }
    return out


def load_rows(directory: Path, start: date, end: date) -> tuple[list[dict], int]:
    rows: list[dict] = []
    bad = 0
    day = start
    while day <= end:
        path = directory / f"{day.isoformat()}.jsonl"
        day += timedelta(days=1)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(row, dict) or _parse_ts(row.get("ts")) is None:
                bad += 1
                continue
            rows.append(row)
    return rows, bad


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _table(title: str, summary: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    lines = [f"## {title}"]
    if not summary:
        lines.append("0 件")
        return lines
    lines.append("route / caller | 件数 | wait p50/p95/max (ms) | hold p50/p95/max (ms) | "
                 "timeout | 失敗 | http>=400 | lock state | 欠落率")
    for (route, caller), s in sorted(summary.items()):
        wait, hold = s["wait_ms"], s["hold_ms"]
        states = ",".join(f"{k}={v}" for k, v in sorted(s["lock_states"].items()))
        lines.append(
            f"{route} / {caller} | {s['count']} | "
            f"{_fmt(wait['p50'])}/{_fmt(wait['p95'])}/{_fmt(wait['max'])} | "
            f"{_fmt(hold['p50'])}/{_fmt(hold['p95'])}/{_fmt(hold['max'])} | "
            f"{s['timeouts']} | {s['failures']} | {s['http_errors']} | {states} | "
            f"{s['missing_rate'] * 100:.1f}%")
    return lines


def _as_list(summary: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"route": route, "caller": caller, **value}
            for (route, caller), value in sorted(summary.items())]


def _date_arg(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("YYYY-MM-DD で指定してください") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bridge 受動観測の集計")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="作業 root")
    today = datetime.now(timezone.utc).date()
    parser.add_argument("--from", dest="start", type=_date_arg, default=today - timedelta(days=7),
                        help="開始日 (UTC、既定は 7 日前)")
    parser.add_argument("--to", dest="end", type=_date_arg, default=today,
                        help="終了日 (UTC、既定は今日)")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--from は --to 以前にしてください")
    rows, bad = load_rows(args.root / "logs" / "bridge-metrics", args.start, args.end)
    overall = summarize(rows)
    friday = summarize(friday_close_rows(rows))
    if args.json:
        print(json.dumps({"from": args.start.isoformat(), "to": args.end.isoformat(),
                          "rows": len(rows), "bad_lines": bad,
                          "overall": _as_list(overall), "friday_close": _as_list(friday)},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"期間 {args.start.isoformat()} 〜 {args.end.isoformat()} (UTC)、行 {len(rows)}、"
          f"読めない行 {bad}")
    print("\n".join(_table("全体", overall)))
    print("\n".join(_table("金曜閉場 (閉場の 1 時間前から閉場まで)", friday)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

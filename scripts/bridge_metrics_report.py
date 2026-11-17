#!/usr/bin/env python3
"""bridge の要求ごとの記録 (bridge 側) と client 側の観測を集計する。

bridge 側 file (`logs/bridge-metrics.jsonl` と日付つきのローテーション済み) と client 側 file
(`logs/bridge-metrics/client.jsonl` と CLI 用の `client-cli.jsonl`、それぞれ同様) を別々に、endpoint x caller ごとに件数・lock の
待ち/占有の p50/p95/最大・timeout と失敗の件数・lock state 別の件数・欠落率で出す。期間は
各行の ts (UTC) で絞る。金曜の閉場の 1 時間前から閉場までの行は別の表にする。閉場は
`agentic_fx.core.market_hours` の開から閉への実際の境界で判定する (休日は境界にならない)。
read-only。bridge には何も要求しない。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from agentic_fx.core import market_hours
except ImportError:  # リポジトリから直接実行するとき
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from agentic_fx.core import market_hours

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


def friday_close_utc(day: date) -> datetime | None:
    """金曜に市場が開から閉へ変わる UTC 時刻。金曜でない日・休日で既に閉場の日は None。"""
    if day.weekday() != 4:
        return None
    for hour in (21, 22):
        edge = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
        if (market_hours.is_market_open(edge - timedelta(microseconds=1))
                and not market_hours.is_market_open(edge)):
            return edge
    return None


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
            "missing_rate": lost / len(items),
            "dropped_records": sum(int(i["dropped_before"]) for i in items
                                   if isinstance(i.get("dropped_before"), int)
                                   and not isinstance(i["dropped_before"], bool)
                                   and i["dropped_before"] > 0),
            "wait_ms": _stats(waits), "hold_ms": _stats(holds),
        }
    return out


def _source_files(base: Path) -> list[Path]:
    """書込み中の file と、`<名前>.YYYY-MM-DD` のローテーション済み file。"""
    pattern = re.compile(re.escape(base.name) + r"\.\d{4}-\d{2}-\d{2}")
    try:
        names = sorted(p for p in base.parent.iterdir() if pattern.fullmatch(p.name))
    except OSError:
        names = []
    return names + [base]


def load_rows(base: Path | list[Path], start: date, end: date) -> tuple[list[dict], int]:
    """行の ts が [start 0:00Z, end の翌日 0:00Z) のものだけを返す。読めない行は件数に数える。

    `base` は書込み中の file (複数可)。それぞれのローテーション済みも読む。"""
    lower = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    upper = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    rows: list[dict] = []
    bad = 0
    bases = base if isinstance(base, list) else [base]
    for path in [p for one in bases for p in _source_files(one)]:
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
            moment = _parse_ts(row.get("ts")) if isinstance(row, dict) else None
            if moment is None:
                bad += 1
                continue
            if lower <= moment < upper:
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
                 "timeout | 失敗 | http>=400 | lock state | 欠落率 (lock header) | 記録の欠落 (捨てた行)")
    for (route, caller), s in sorted(summary.items()):
        wait, hold = s["wait_ms"], s["hold_ms"]
        states = ",".join(f"{k}={v}" for k, v in sorted(s["lock_states"].items()))
        lines.append(
            f"{route} / {caller} | {s['count']} | "
            f"{_fmt(wait['p50'])}/{_fmt(wait['p95'])}/{_fmt(wait['max'])} | "
            f"{_fmt(hold['p50'])}/{_fmt(hold['p95'])}/{_fmt(hold['max'])} | "
            f"{s['timeouts']} | {s['failures']} | {s['http_errors']} | {states} | "
            f"{s['missing_rate'] * 100:.1f}% | {s['dropped_records']}")
    return lines


def _as_list(summary: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"route": route, "caller": caller, **value}
            for (route, caller), value in sorted(summary.items())]


def _date_arg(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("YYYY-MM-DD で指定してください") from None


def _side(label: str, base: Path | list[Path], start: date, end: date) -> dict[str, Any]:
    rows, bad = load_rows(base, start, end)
    return {"label": label, "rows": rows, "bad": bad,
            "overall": summarize(rows), "friday": summarize(friday_close_rows(rows))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bridge の記録 (bridge 側・client 側) の集計")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="作業 root")
    parser.add_argument("--bridge-file", type=Path, default=None,
                        help="bridge 側の file (既定: <root>/mt5_bridge/logs/bridge-metrics.jsonl)")
    parser.add_argument("--client-file", type=Path, action="append", default=None,
                        help="client 側の file (複数指定可。既定: <root>/logs/bridge-metrics/ の "
                             "client.jsonl と client-cli.jsonl)")
    today = datetime.now(timezone.utc).date()
    parser.add_argument("--from", dest="start", type=_date_arg, default=today - timedelta(days=7),
                        help="開始日 (UTC、既定は 7 日前)")
    parser.add_argument("--to", dest="end", type=_date_arg, default=today,
                        help="終了日 (UTC、既定は今日)")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--from は --to 以前にしてください")
    bridge_file = args.bridge_file or args.root / "mt5_bridge" / "logs" / "bridge-metrics.jsonl"
    metrics_dir = args.root / "logs" / "bridge-metrics"
    client_file = args.client_file or [metrics_dir / "client.jsonl",
                                       metrics_dir / "client-cli.jsonl"]
    sides = {"bridge": _side("bridge 側", bridge_file, args.start, args.end),
             "client": _side("client 側", client_file, args.start, args.end)}
    if args.json:
        print(json.dumps({
            "from": args.start.isoformat(), "to": args.end.isoformat(),
            **{key: {"rows": len(side["rows"]), "bad_lines": side["bad"],
                     "overall": _as_list(side["overall"]),
                     "friday_close": _as_list(side["friday"])}
               for key, side in sides.items()}},
            ensure_ascii=False, indent=2))
        return 0
    print(f"期間 {args.start.isoformat()} 〜 {args.end.isoformat()} (UTC)")
    for side in sides.values():
        print(f"\n# {side['label']}: 行 {len(side['rows'])}、読めない行 {side['bad']}")
        print("\n".join(_table(f"{side['label']} 全体", side["overall"])))
        print("\n".join(_table(f"{side['label']} 金曜閉場 (閉場の 1 時間前から閉場まで)",
                              side["friday"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())

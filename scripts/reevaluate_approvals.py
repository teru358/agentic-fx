#!/usr/bin/env python3
"""保存済み承認を、固定した収益性フロアで読み取り専用再評価する。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from agentic_fx.config import ImproveGateSettings
from agentic_fx.plugin.strategy_gate import _check_profitability_floor


MIN_TRADES = 30
_GATE = ImproveGateSettings(
    min_pf=1.0,
    require_positive_avg_r=True,
    require_holdout_evaluable=True,
)
_SETTINGS = SimpleNamespace(improve=SimpleNamespace(gate=_GATE))


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open SQLite without a write-capable handle, then pin the connection read-only."""
    database_uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(database_uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _number(value: object) -> bool:
    return type(value) in (int, float)


def _metric(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    trades, pf, avg_r = raw.get("trades"), raw.get("pf"), raw.get("avg_r")
    if type(trades) is not int or trades < 0:
        return None
    if pf is not None and not _number(pf):
        return None
    if avg_r is not None and not _number(avg_r):
        return None
    evaluable = raw.get("evaluable")
    if evaluable is not None and type(evaluable) is not bool:
        return None
    return {
        "trades": trades,
        "pf": pf,
        "avg_r": avg_r,
        "evaluable": trades >= MIN_TRADES if evaluable is None else evaluable,
    }


def _metrics(value: object, *, in_sample_pairs: object = ()) -> dict | None:
    """Normalize pair-keyed and single-pair flat saved metrics for the gate."""
    if not isinstance(value, dict) or not value:
        return None
    if all(isinstance(raw, dict) for raw in value.values()):
        result: dict = {}
        for pair, raw in value.items():
            metric = _metric(raw)
            if not isinstance(pair, str) or metric is None:
                return None
            result[pair] = metric
        return result
    metric = _metric(value)
    if metric is None:
        return None
    pairs = tuple(in_sample_pairs) if isinstance(in_sample_pairs, dict) else ()
    pair = pairs[0] if len(pairs) == 1 else "*"
    return {pair: metric}


def _value(metrics: dict, key: str) -> str:
    values = [f"{pair}:{item[key]}" for pair, item in metrics.items()]
    return ", ".join(values)


def _strategy(payload: dict) -> str:
    name = payload.get("name")
    content_hash = payload.get("content_hash")
    if not isinstance(name, str) or not name or not isinstance(content_hash, str):
        return "-"
    return f"{name}@{content_hash[:12]}"


def _verdict(in_sample: dict, holdout: dict) -> str:
    in_label, _ = _check_profitability_floor(
        in_sample, settings=_SETTINGS, scope="in_sample")
    holdout_label, holdout_detail = _check_profitability_floor(
        holdout, settings=_SETTINGS, scope="holdout")
    pf_failed = bool(in_label) or (bool(holdout_label)
                                  and "holdout_not_evaluable" not in holdout_detail)
    holdout_unavailable = "holdout_not_evaluable" in holdout_detail
    if pf_failed and holdout_unavailable:
        return "両方"
    if pf_failed:
        return "PF 不足"
    if holdout_unavailable:
        return "holdout 評価不能"
    return "合格"


def _ids(text: str | None) -> list[int] | None:
    if text is None:
        return None
    result: list[int] = []
    for part in text.split(","):
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0:
            result.append(value)
    return result


def rows(conn: sqlite3.Connection, ids: list[int] | None):
    if ids is None:
        return conn.execute(
            "SELECT id, payload_json FROM approval_requests "
            "WHERE status='approved' ORDER BY id")
    if not ids:
        return ()
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT id, payload_json FROM approval_requests WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    )


def render(conn: sqlite3.Connection, ids: list[int] | None) -> list[str]:
    output = ["承認 id | 戦略名と版 | in-sample pf / avg_r | holdout pf / 取引数 | 判定"]
    for row in rows(conn, ids):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            payload = {}
        in_sample = _metrics(payload.get("in_sample"))
        holdout = _metrics(payload.get("holdout"), in_sample_pairs=in_sample)
        if in_sample is None or holdout is None:
            output.append(f"{row['id']} | {_strategy(payload)} | - | - | 評価不能 (値なし)")
            continue
        output.append(
            f"{row['id']} | {_strategy(payload)} | pf={_value(in_sample, 'pf')} / "
            f"avg_r={_value(in_sample, 'avg_r')} | pf={_value(holdout, 'pf')} / "
            f"trades={_value(holdout, 'trades')} | {_verdict(in_sample, holdout)}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/agentic.db", type=Path)
    parser.add_argument("--ids")
    try:
        args, _unknown = parser.parse_known_args(argv)
        conn = open_readonly(args.db)
        try:
            print("\n".join(render(conn, _ids(args.ids))))
        finally:
            conn.close()
    except BaseException as exc:  # 提示コマンドなので、失敗も判定扱いにしない。
        print(f"再評価を実行できません: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

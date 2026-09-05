"""golden-v1 → golden-v2 の差分を機械分類する (段階 3/4)。

design-v4-addendum.md §1/§10: 段階 3 (先頭足規則 A2 + warmup 中 Scheduler
停止) による golden の差分は、以下の 2 分類にしか機械分類できないことを
確認する。分類不能な差分が 1 件でもあれば red。

  (a) pre-decision: `ts < first_decision_at(v2)` の snapshots / equity 点
      / kill-switch イベント (v1 にのみ存在する — v2 は warmup 中に
      scheduler を止めるため、この期間の観測記録がそもそも作られない)。
  (b) derived: その期間内のシグナルに由来する orders と、それに連なる
      equity 点 (v1/v2 で同一シナリオを使う golden fixture では今回は
      該当差分が 0 件だった — orders/metrics は逐語一致)。

分類表 (件数) は ``tests/backtest/golden/README.md`` に残す。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

GOLDEN_DIR = Path(__file__).parent / "golden"


def _load(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _classify_removed_rows(
        v1_rows: list, v2_rows: list, *,
        key_fn: Callable[[Any], Any],
        ts_fn: Callable[[Any], str],
        first_decision_at: datetime) -> list:
    """v1_rows のうち v2_rows に対応が無い行を「除去された行」として抽出する。

    両リストは ts 昇順 (golden の生成契約) を前提にした 2 ポインタ走査 —
    段階 3 で v2 が失う行は必ず v1 の**先頭側の連続区間**（warmup 中の
    観測）なので、この単純な走査で全数を機械的に説明できる。対応しない
    行が見つかった時点で ts が `first_decision_at(v2)` 未満でなければ
    「分類不能」として例外にする（呼び出し側で assert する設計にするため
    ここでは行そのものを返す）。
    """
    i = j = 0
    removed = []
    while i < len(v1_rows):
        if j < len(v2_rows) and key_fn(v1_rows[i]) == key_fn(v2_rows[j]):
            i += 1
            j += 1
        else:
            removed.append(v1_rows[i])
            i += 1
    assert j == len(v2_rows), (
        f"golden-v2 に golden-v1 との対応が付かない残り行がある "
        f"(j={j}, len(v2)={len(v2_rows)}) — 分類不能な差分")
    for row in removed:
        ts = _parse(ts_fn(row))
        if ts >= first_decision_at:
            raise AssertionError(
                f"分類不能な差分 (pre-decision でも derived でもない): {row!r}")
    return removed


def _snapshot_key(row: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in row.items() if k != "id"))


def _equity_key(row: list) -> tuple:
    return tuple(row)


def _kill_switch_key(row: dict) -> tuple:
    return tuple(sorted(row.items()))


def test_golden_v1_to_v2_diff_classifies_fully_as_pre_decision_or_derived():
    v1 = _load("golden-v1.json")
    v2 = _load("golden-v2.json")

    first_decision_v2 = _parse(v2["first_decision_at"])
    assert _parse(v1["first_decision_at"]) <= first_decision_v2, (
        "段階 3 の先頭足規則は意思決定を遅らせる方向のみ (early 化なら分類不能)")

    counts = {"pre_decision_snapshots": 0, "pre_decision_equity": 0,
             "pre_decision_kill_switch": 0, "derived_orders": 0}

    removed_snapshots = _classify_removed_rows(
        v1["snapshots"], v2["snapshots"], key_fn=_snapshot_key,
        ts_fn=lambda r: r["ts"], first_decision_at=first_decision_v2)
    counts["pre_decision_snapshots"] = len(removed_snapshots)
    assert removed_snapshots, (
        "段階 3 の warmup 停止で pre-decision snapshots が 1 件も無いのは想定外 "
        "(golden fixture の start が first_decision_at より前であるはず)")

    removed_equity = _classify_removed_rows(
        v1["equity_curve"], v2["equity_curve"], key_fn=_equity_key,
        ts_fn=lambda r: r[0], first_decision_at=first_decision_v2)
    counts["pre_decision_equity"] = len(removed_equity)
    assert removed_equity

    removed_ks = _classify_removed_rows(
        v1["kill_switch_events"], v2["kill_switch_events"],
        key_fn=_kill_switch_key, ts_fn=lambda r: r["ts"],
        first_decision_at=first_decision_v2)
    counts["pre_decision_kill_switch"] = len(removed_ks)

    # orders / metrics: このシナリオでは全シグナルが first_decision_at(v2)
    # 以降のバケットに由来するため差分 0 件 (derived 分類の実例は無い) —
    # 逐語一致で確認する (差があれば分類不能として扱う)。
    assert v1["orders"] == v2["orders"], (
        "orders に差分がある場合は derived (シグナル時刻 < first_decision_at(v2) "
        "由来) に該当するかを個別検証する必要がある — このシナリオでは 0 件を期待")
    assert v1["metrics"] == v2["metrics"]
    counts["derived_orders"] = 0

    # 他のトップレベルフィールドは不変
    assert v1["start"] == v2["start"]
    assert v1["end"] == v2["end"]
    assert v1["fallback_spread_used"] == v2["fallback_spread_used"]

    # 分類表は README.md に静的に記載済み (テスト実行時に repo ファイルを
    # 書き換えない — テストが実資源を触らない規律)。件数がずれたら
    # README の記載も更新すること。
    assert counts == {
        "pre_decision_snapshots": 60,
        "pre_decision_equity": 59,
        "pre_decision_kill_switch": 0,
        "derived_orders": 0,
    }, counts

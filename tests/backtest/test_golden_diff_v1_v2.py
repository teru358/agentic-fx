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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from agentic_fx.backtest.runner import parse_timeframe
from agentic_fx.backtest.timeframes import ceil_to_bucket

GOLDEN_DIR = Path(__file__).parent / "golden"

# golden/generate.py の run_replay 呼び出しが使う eval_timeframe (段階 3/4
# 生成時点でのハードコード値と一致させる — 単一の真実源が golden.json 自身
# には無いため、生成スクリプトの引数をここに転記する)。
_GOLDEN_EVAL_TIMEFRAME = "1h"


def _load(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _expected_first_decision_at(start: datetime, eval_timeframe: str) -> datetime:
    """I3 是正 (codex 段階2/3 是正 1周目): `run_replay` (runner.py:220)
    と**同じ**式 (`ceil_to_bucket(start, eval_timeframe) + parse_timeframe
    (eval_timeframe)`) で独立に境界を計算する — golden-v2 自身が名乗る
    `first_decision_at` を無条件に信じない。旧分類器は
    `first_decision_at(v1) <= first_decision_at(v2)` の順序関係しか見て
    おらず、v2 の意思決定開始そのものが誤って (例えば bucket 1 個分)
    遅延していても、対応する先頭行を削った偽 golden を pre-decision 分類
    として無条件に受理してしまっていた。"""
    return ceil_to_bucket(start, eval_timeframe) + parse_timeframe(eval_timeframe)


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


def _order_key(row: dict) -> tuple:
    # id/intent_id は行番号採番 (derived 分類で先頭の注文が抜けると後続の
    # 番号が全てずれる) — 番号非依存の内容だけで対応付ける。
    return tuple(sorted((k, v) for k, v in row.items()
                        if k not in ("id", "intent_id")))


def _classify_golden_diff(v1: dict, v2: dict, *, eval_timeframe: str) -> dict:
    """golden-v1 → golden-v2 の全差分を機械分類し、件数表を返す。分類不能
    な差分 (pre-decision でも derived でもない) が 1 件でもあれば
    AssertionError (呼び出し元 assert ではなく本関数内で fail closed —
    フォーカステストからも直接呼べるよう分離した、I3 是正)。"""
    first_decision_v2 = _parse(v2["first_decision_at"])
    assert _parse(v1["first_decision_at"]) <= first_decision_v2, (
        "段階 3 の先頭足規則は意思決定を遅らせる方向のみ (early 化なら分類不能)")
    # I3 是正 (codex 段階2/3 是正 1周目): v2 自身が名乗る first_decision_at
    # を無条件に信じず、run_replay と同じ式で独立に再計算した値と一致する
    # ことを確認する — v2 の意思決定開始自体が誤って追加で遅延していても
    # (対応する先頭行さえ削れていれば) 旧分類器は無条件に受理していた。
    assert first_decision_v2 == _expected_first_decision_at(
        _parse(v2["start"]), eval_timeframe), (
        "golden-v2 の first_decision_at が run_replay の式 "
        "(ceil_to_bucket(start, eval_timeframe) + eval_timeframe 幅) と "
        "一致しない — 意図しない追加遅延の疑い")

    counts = {"pre_decision_snapshots": 0, "pre_decision_equity": 0,
             "pre_decision_kill_switch": 0, "derived_orders": 0}

    removed_snapshots = _classify_removed_rows(
        v1["snapshots"], v2["snapshots"], key_fn=_snapshot_key,
        ts_fn=lambda r: r["ts"], first_decision_at=first_decision_v2)
    counts["pre_decision_snapshots"] = len(removed_snapshots)

    removed_equity = _classify_removed_rows(
        v1["equity_curve"], v2["equity_curve"], key_fn=_equity_key,
        ts_fn=lambda r: r[0], first_decision_at=first_decision_v2)
    counts["pre_decision_equity"] = len(removed_equity)

    removed_ks = _classify_removed_rows(
        v1["kill_switch_events"], v2["kill_switch_events"],
        key_fn=_kill_switch_key, ts_fn=lambda r: r["ts"],
        first_decision_at=first_decision_v2)
    counts["pre_decision_kill_switch"] = len(removed_ks)

    # I3 是正: 逐語比較ではなく `_classify_removed_rows` (created_at <
    # first_decision_at(v2) の注文のみ derived として許容) を経由させる
    # ことで、実際に derived 差分が出た場合も機械分類できるようにする。
    removed_orders = _classify_removed_rows(
        v1["orders"], v2["orders"], key_fn=_order_key,
        ts_fn=lambda r: r["created_at"], first_decision_at=first_decision_v2)
    counts["derived_orders"] = len(removed_orders)
    assert v1["metrics"] == v2["metrics"]

    # 他のトップレベルフィールドは不変
    assert v1["start"] == v2["start"]
    assert v1["end"] == v2["end"]
    assert v1["fallback_spread_used"] == v2["fallback_spread_used"]
    return counts


def test_golden_v1_to_v2_diff_classifies_fully_as_pre_decision_or_derived():
    v1 = _load("golden-v1.json")
    v2 = _load("golden-v2.json")

    counts = _classify_golden_diff(v1, v2, eval_timeframe=_GOLDEN_EVAL_TIMEFRAME)
    assert counts["pre_decision_snapshots"] > 0, (
        "段階 3 の warmup 停止で pre-decision snapshots が 1 件も無いのは想定外 "
        "(golden fixture の start が first_decision_at より前であるはず)")
    assert counts["pre_decision_equity"] > 0

    # 分類表は README.md に静的に記載済み (テスト実行時に repo ファイルを
    # 書き換えない — テストが実資源を触らない規律)。件数がずれたら
    # README の記載も更新すること。
    assert counts == {
        "pre_decision_snapshots": 60,
        "pre_decision_equity": 59,
        "pre_decision_kill_switch": 0,
        "derived_orders": 0,
    }, counts


# --- I3 是正 (codex 段階2/3 是正 1周目): 分類器のフォーカステスト --------

_HOUR = timedelta(hours=1)
_START = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _minimal_golden(*, start, first_decision_at, snapshots, equity_curve,
                    orders) -> dict:
    return {
        "start": start.isoformat(), "end": (start + _HOUR * 5).isoformat(),
        "first_decision_at": first_decision_at.isoformat(),
        "snapshots": snapshots, "equity_curve": equity_curve,
        "kill_switch_events": [], "orders": orders,
        "metrics": {"trades": 0}, "fallback_spread_used": False,
    }


def test_fake_golden_with_delayed_first_decision_at_is_rejected():
    """v2 の `first_decision_at` を意図的に 1 bucket (1h) 遅らせ、対応する
    先頭行 (snapshot 1 件) を削った偽 golden を作る。旧分類器は
    `first_decision_at(v1) <= first_decision_at(v2)` の順序関係と
    `ts < first_decision_at(v2)` しか見ていなかったため、この偽 golden も
    「pre-decision」として無条件に受理してしまっていた (先頭行が削れて
    さえいれば `_classify_removed_rows` は j==len(v2) を満たし、削られた
    行の ts も delayed_first_decision 未満なので通ってしまう)。
    `_classify_golden_diff` (メイン契約テストと同じ関数) がこの偽 golden
    を拒否することを、実際に関数を呼んで確認する。
    """
    correct_first_decision = _expected_first_decision_at(_START, "1h")
    delayed_first_decision = correct_first_decision + _HOUR

    v1 = _minimal_golden(
        start=_START, first_decision_at=correct_first_decision,
        snapshots=[
            {"ts": _START.isoformat(), "equity": 1_000_000.0},
            {"ts": correct_first_decision.isoformat(), "equity": 1_000_000.0},
            {"ts": delayed_first_decision.isoformat(), "equity": 1_000_000.0}],
        equity_curve=[], orders=[])
    # 偽 v2: correct_first_decision の行**まで**削り、first_decision_at を
    # 1 bucket 遅らせて自己申告する (本来消えるべきは _START の 1 行だけ)。
    v2_fake = _minimal_golden(
        start=_START, first_decision_at=delayed_first_decision,
        snapshots=[
            {"ts": delayed_first_decision.isoformat(), "equity": 1_000_000.0}],
        equity_curve=[], orders=[])

    with pytest.raises(AssertionError, match="意図しない追加遅延"):
        _classify_golden_diff(v1, v2_fake, eval_timeframe="1h")


def test_real_shaped_golden_diff_classifies_correctly_with_correct_boundary():
    """上のテストと対になる陽性経路: 正しい first_decision_at・正しい
    削除行数 (1 本だけ) の golden なら `_classify_golden_diff` が
    受理し、正しい件数を返すこと。"""
    correct_first_decision = _expected_first_decision_at(_START, "1h")
    v1 = _minimal_golden(
        start=_START, first_decision_at=_START,
        snapshots=[
            {"ts": _START.isoformat(), "equity": 1_000_000.0},
            {"ts": correct_first_decision.isoformat(), "equity": 1_000_000.0}],
        equity_curve=[], orders=[])
    v2 = _minimal_golden(
        start=_START, first_decision_at=correct_first_decision,
        snapshots=[
            {"ts": correct_first_decision.isoformat(), "equity": 1_000_000.0}],
        equity_curve=[], orders=[])

    counts = _classify_golden_diff(v1, v2, eval_timeframe="1h")
    assert counts["pre_decision_snapshots"] == 1


def test_synthetic_pre_decision_snapshot_removal_classifies_and_matches_ground_truth():
    """delayed_first_decision_at 自体は _expected_first_decision_at との
    不一致で reject される (上のテストで確認済み) — ここでは、正しい
    first_decision_at を使う限り、その直前の snapshot が「pre-decision」
    として正しく分類されることを確認する (回帰: 分類器がそもそも動く)。
    """
    first_decision = _expected_first_decision_at(_START, "1h")
    v1_snapshots = [
        {"ts": _START.isoformat(), "equity": 1_000_000.0},
        {"ts": first_decision.isoformat(), "equity": 1_000_000.0},
    ]
    v2_snapshots = [{"ts": first_decision.isoformat(), "equity": 1_000_000.0}]

    removed = _classify_removed_rows(
        v1_snapshots, v2_snapshots, key_fn=_snapshot_key,
        ts_fn=lambda r: r["ts"], first_decision_at=first_decision)
    assert len(removed) == 1
    assert removed[0]["ts"] == _START.isoformat()


def test_snapshot_removed_at_first_decision_is_unclassifiable():
    """C7-1: first_decision_at ちょうどの除去を pre-decision に緩和しない。"""
    first_decision = _expected_first_decision_at(_START, "1h")
    with pytest.raises(AssertionError, match="分類不能"):
        _classify_removed_rows(
            [{"ts": first_decision.isoformat(), "equity": 1_000_000.0}], [],
            key_fn=_snapshot_key, ts_fn=lambda row: row["ts"],
            first_decision_at=first_decision)


def test_synthetic_derived_order_from_early_signal_classifies_as_b():
    """(b) derived の実例: pre-decision 期間内 (`created_at <
    first_decision_at(v2)`) のシグナルに由来する order が v1 にのみ存在し、
    v2 では対応する評価そのものが行われない (warmup 中は Scheduler が
    止まる) ため order が生成されない場合、これを derived として分類
    できることを合成 fixture で確認する (実 golden はこのケースが 0 件)。
    """
    first_decision = _expected_first_decision_at(_START, "1h")
    early_order = {
        "id": 1, "intent_id": 1, "created_at": _START.isoformat(),
        "pair": "USDJPY", "direction": "long", "status": "closed",
    }
    later_order = {
        "id": 2, "intent_id": 2,
        "created_at": (first_decision + _HOUR).isoformat(),
        "pair": "USDJPY", "direction": "short", "status": "closed",
    }
    v1_orders = [early_order, later_order]
    # v2: id 再採番されるが内容 (id/intent_id 以外) は同一 — early_order の
    # 評価そのものが起きないため欠落する。
    v2_orders = [{**later_order, "id": 1, "intent_id": 1}]

    removed = _classify_removed_rows(
        v1_orders, v2_orders, key_fn=_order_key,
        ts_fn=lambda r: r["created_at"], first_decision_at=first_decision)
    assert len(removed) == 1
    assert removed[0]["created_at"] == _START.isoformat()


def test_synthetic_order_removed_after_first_decision_at_is_unclassifiable():
    """first_decision_at(v2) 以降に created_at を持つ order が v2 から
    消えている場合は「分類不能」— derived でも pre-decision でもない実
    regression (例えば実行ロジックのバグで注文が握り潰された) を分類器が
    誤って見逃さないこと。"""
    first_decision = _expected_first_decision_at(_START, "1h")
    suspicious_order = {
        "id": 1, "intent_id": 1,
        "created_at": (first_decision + _HOUR).isoformat(),
        "pair": "USDJPY", "direction": "long", "status": "closed",
    }
    with pytest.raises(AssertionError, match="分類不能"):
        _classify_removed_rows(
            [suspicious_order], [], key_fn=_order_key,
            ts_fn=lambda r: r["created_at"], first_decision_at=first_decision)

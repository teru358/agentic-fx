"""再評価の依頼の表示。承認の詳細 (ctl / shell / web / 承認カード) の材料と固定文言。

web と承認カードは afx-ops (別 package) が応答の `reevaluation` から描く。ここで作る
view は数値だけの構造で、文字列は持たない。表示の文言 (却下の結果の 5 形など) の
正本はこの module で、shell と ctl はここから読む。
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable

from agentic_fx.loops import approval_facts

ORIGIN = "reevaluation"
SOURCES = ("payload", "run", "unknown")
SCOPES = ("in_sample", "holdout")
METRIC_KEYS = ("pf", "trades", "avg_r", "max_drawdown")

STALE_TEXT = ("依頼が古いため無効にしました。配備は変えていません。"
              "再評価をやり直してください")
EPOCH_TEXT = "履歴が変わったため決定していません"
CONFIRMED_TEXT = "再評価の結果で承認の根拠を差し替えました (配備は変わりません)"
REJECT_TEXT = {
    "done": "却下し、配備を停止しました",
    "needs_retry": ("却下しました。取引は止まりましたが symlink を外せていません。"
                    "`afx plugin disable {name}` で完了してください"),
    "foreign": ("却下しました。取引は止まりましたが plugins/{name} が想定と違うため"
                "外していません。確認してください"),
}
STOP_HINT = "配備を止めるなら `afx plugin disable {name}`"
STALE_STOP_TEXT = STALE_TEXT + "。" + STOP_HINT
EPOCH_STOP_TEXT = EPOCH_TEXT + "。" + STOP_HINT
_SOURCE_TEXT = {"payload": "承認時の記録", "unknown": "不明"}


def reject_warning(name: str, verb: str = "却下") -> str:
    """再評価の依頼を却下する前に見せる文。"""
    return f"{verb}すると strategy {name} の配備を停止します (position は保護を続けます)"


def reject_text(name: str, deploy_phase: str | None) -> str | None:
    """却下の結果 (停止の段階) の文言。知らない段階は None。"""
    tail = REJECT_TEXT.get(deploy_phase)
    return None if tail is None else tail.format(name=name)


def _number(value: object) -> bool:
    return (type(value) is int) or (type(value) is float and math.isfinite(value))


def _pick(metrics: object) -> dict | None:
    if not isinstance(metrics, dict):
        return None
    return {key: metrics[key] for key in METRIC_KEYS if _number(metrics.get(key))}


def _pair_metrics(value: object, pair: str, pair_count: int) -> object:
    if not isinstance(value, dict) or not value:
        return None
    if "trades" in value:
        return value if pair_count == 1 else None
    return value.get(pair)


def build_view(conn: sqlite3.Connection, payload: dict) -> dict | None:
    """再評価の依頼の payload から、表示の材料を作る。再評価でなければ None。"""
    if payload.get("candidate_origin") != ORIGIN:
        return None
    pairs = payload.get("pairs")
    if not (isinstance(pairs, list) and all(isinstance(p, str) for p in pairs)):
        pairs = []
    previous = payload.get("previous") if isinstance(payload.get("previous"), dict) else {}
    previous_id = previous.get("approval_id")
    if type(previous_id) is not int:
        previous_id = payload.get("reevaluates_approval_id")
    previous_id = previous_id if type(previous_id) is int else None
    previous_at = None
    if previous_id is not None:
        row = conn.execute("SELECT decided_at, created_at FROM approval_requests WHERE id=?",
                           (previous_id,)).fetchone()
        if row is not None:
            previous_at = row["decided_at"] or row["created_at"]
    rows = []
    for pair in pairs:
        for scope in SCOPES:
            before = (previous.get(scope) or {}).get(pair) if isinstance(
                previous.get(scope), dict) else None
            before = before if isinstance(before, dict) else {}
            metrics = _pick(before.get("metrics"))
            source = before.get("source") if before.get("source") in SOURCES else "unknown"
            run_id = before.get("run_id")
            rows.append({
                "pair": pair, "scope": scope,
                "previous": {"metrics": metrics,
                             "source": source if metrics is not None else "unknown",
                             "run_id": (run_id if type(run_id) is int and metrics is not None
                                        and source == "run" else None)},
                "current": _pick(_pair_metrics(payload.get(scope), pair, len(pairs)))})
    name = payload.get("name")
    eval_now = payload.get("eval_now")
    return {
        "name": name if isinstance(name, str) else None,
        "reevaluates_approval_id": previous_id,
        "previous_at": previous_at if isinstance(previous_at, str) else None,
        "eval_now": eval_now if isinstance(eval_now, str) else None,
        "rows": rows}


def _rounded(value: object) -> str | None:
    """int はそのまま、有限の float は有効 4 桁。数でなければ None。"""
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return f"{value:.4g}"
    return None


def _cells(metrics: dict | None) -> str:
    if metrics is None:
        return "不明"
    return " ".join(f"{key}={approval_facts.num_text(_rounded(metrics.get(key)))}"
                    for key in METRIC_KEYS)


def row_text(row: dict) -> str:
    before = row.get("previous") if isinstance(row.get("previous"), dict) else {}
    metrics = before.get("metrics") if isinstance(before.get("metrics"), dict) else None
    if metrics is None:
        before_text = "不明"
    else:
        origin = (f"run #{before['run_id']}"
                  if before.get("source") == "run" and type(before.get("run_id")) is int
                  else _SOURCE_TEXT.get(before.get("source"), "不明"))
        before_text = f"{_cells(metrics)} ({origin})"
    current = row.get("current")
    now = _cells(current) if isinstance(current, dict) else "-"
    return (f"{approval_facts.display_text(row.get('pair'))} "
            f"{approval_facts.display_text(row.get('scope'))}: "
            f"前回 {before_text} → 今回 {now}")


def lines(view: object, when: Callable[[str], str]) -> list[str]:
    """承認の詳細に足す行。`when` は ISO の時刻を利用者 tz の表記にする。"""
    if not isinstance(view, dict):
        return []
    previous_id = view.get("reevaluates_approval_id")
    head = "再評価" + (f" (前回の承認 #{previous_id})" if type(previous_id) is int else "")
    previous_at, eval_now = view.get("previous_at"), view.get("eval_now")
    out = [head, "コード変更なし。承認すると承認の根拠だけを差し替え、配備は変わりません"]
    name = view.get("name")
    if isinstance(name, str):
        out.append(reject_warning(approval_facts.display_text(name)))
    out.append("評価時点: 前回 "
               + (when(previous_at) if isinstance(previous_at, str) else "-")
               + " / 今回 " + (when(eval_now) if isinstance(eval_now, str) else "-")
               + " (期間が違う比較です)")
    for row in view.get("rows") or []:
        if isinstance(row, dict):
            out.append(row_text(row))
    return out

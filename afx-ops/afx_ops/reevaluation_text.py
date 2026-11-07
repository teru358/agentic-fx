"""再評価の依頼の表示の材料と固定文言 (web と承認カードが共有する)。

材料は daemon の承認詳細の `reevaluation` (数値と固定語だけの構造)。文言の正本は daemon 側
(`agentic_fx/ops/reevaluation_view.py`) で、afx-ops は本体を import しないので写しを持つ。
両 package の文言一致は tests/ops の 2 package 検査が pin する。
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

ORIGIN = "reevaluation"
SCOPES = ("in_sample", "holdout")
METRIC_KEYS = ("pf", "trades", "avg_r", "max_drawdown")
PHASES = ("done", "needs_retry", "foreign")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

HEADING = "再評価"
NO_CODE_CHANGE = "コード変更なし。承認すると承認の根拠だけを差し替え、配備は変わりません"
STOP_HINT = "配備を止めるなら `afx plugin disable {name}`"
PERIOD_NOTE = "(期間が違う比較です)"
STALE_TEXT = ("依頼が古いため無効にしました。配備は変えていません。"
              "再評価をやり直してください")
CONFIRMED_TEXT = "根拠を差し替えました (配備は変わりません)"
REJECT_TEXT = {
    "done": "却下し、配備を停止しました",
    "needs_retry": ("却下しました。取引は止まりましたが symlink を外せていません。"
                    "`afx plugin disable {name}` で完了してください"),
    "foreign": ("却下しました。取引は止まりましたが plugins/{name} が想定と違うため"
                "外していません。確認してください"),
}
_SOURCE_TEXT = {"payload": "承認時の記録", "unknown": "不明"}


def reject_warning(name: str, verb: str = "Reject ") -> str:
    return f"{verb}すると strategy {name} の配備を停止します (position は保護を続けます)"


def reject_text(name: str, phase: object) -> str | None:
    tail = REJECT_TEXT.get(phase) if type(phase) is str else None
    return None if tail is None else tail.format(name=name)


def is_reevaluation(detail: object) -> bool:
    return type(detail) is dict and detail.get("candidate_origin") == ORIGIN


def safe_name(value: object) -> str | None:
    return value if type(value) is str and _NAME.fullmatch(value) else None


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _cells(metrics: object) -> str:
    if type(metrics) is not dict:
        return "不明"
    return " ".join(f"{key}={metrics[key] if _number(metrics.get(key)) else '-'}"
                    for key in METRIC_KEYS)


def _row_text(row: dict) -> str:
    before = row.get("previous") if type(row.get("previous")) is dict else {}
    metrics = before.get("metrics")
    if type(metrics) is not dict:
        before_text = "不明"
    else:
        source = before.get("source")
        run_id = before.get("run_id")
        origin = (f"run #{run_id}" if source == "run" and type(run_id) is int
                  else _SOURCE_TEXT.get(source, "不明"))
        before_text = f"{_cells(metrics)} ({origin})"
    current = row.get("current")
    return f"前回 {before_text} → 今回 {_cells(current) if type(current) is dict else '-'}"


@dataclass(frozen=True)
class Description:
    heading: str
    notes: tuple[str, ...]
    rows: tuple[tuple[str, str], ...]


def describe(detail: object, when: Callable[[str], str]) -> Description | None:
    """再評価の依頼の詳細から見出し・注記・比較の行を作る。再評価でない (古い daemon を含む)
    ・形が違うときは None。値は無害化していない (呼び出し側が escape する)。"""
    if not is_reevaluation(detail):
        return None
    view = detail.get("reevaluation")
    if type(view) is not dict:
        return Description(HEADING, (NO_CODE_CHANGE,), ())
    previous_id = view.get("reevaluates_approval_id")
    heading = HEADING + (f" (前回の承認 #{previous_id})" if type(previous_id) is int else "")
    notes = [NO_CODE_CHANGE]
    name = safe_name(view.get("name"))
    if name is not None:
        notes.append(reject_warning(name, "却下"))
    previous_at, eval_now = view.get("previous_at"), view.get("eval_now")
    notes.append("評価時点: 前回 " + (when(previous_at) if type(previous_at) is str else "-")
                 + " / 今回 " + (when(eval_now) if type(eval_now) is str else "-")
                 + " " + PERIOD_NOTE)
    rows = []
    for row in (view.get("rows") if type(view.get("rows")) is list else [])[:16]:
        if type(row) is dict and type(row.get("pair")) is str and row.get("scope") in SCOPES:
            rows.append((f"{row['pair']} {row['scope']}", _row_text(row)))
    return Description(heading, tuple(notes), tuple(rows))

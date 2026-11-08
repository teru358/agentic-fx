"""承認待ちの依頼の「土台」項目。承認の詳細 (ctl / shell / web / 承認カード) の材料と固定文言。

土台の判定は `plugin/base_identity.py`。ここは詳細に載せる形 (`base_check`) と、
ctl と shell が共有する警告文を作る。web と承認カードは afx-ops (別 package) が
応答の `base_check` から描く。
"""
from __future__ import annotations

from pathlib import Path

from agentic_fx.loops.approval_facts import display_text
from agentic_fx.plugin import base_identity, loader

WARNING_STATES = (base_identity.STATE_MISMATCH, base_identity.STATE_NONE_BUT_LIVE,
                  base_identity.STATE_UNREADABLE)
_LIVE_WORDS = {"none": "無し", "unreadable": "読めない"}
_BASE_WORDS = {"unknown": "不明"}


def detail(payload: dict, plugins_root: Path | None, status: object = "pending") -> dict | None:
    """承認待ちの間だけ、候補の土台と今の配備を比べた結果を返す。決定済みは None。"""
    name = payload.get("name")
    if (status != "pending" or plugins_root is None or not isinstance(name, str)
            or not loader.is_canonical_plugin_name(name)):
        return None
    result = base_identity.check(payload, plugins_root, name)
    return {"state": result.state, "base": result.base, "live": result.live}


def warning_line(view) -> str | None:
    """警告が要る状態のときだけ 1 行の文を返す。一致・旧形式・bless は出さない。"""
    if not isinstance(view, dict):
        return None
    state = view.get("state")
    if state == base_identity.STATE_MISMATCH:
        base, live = view.get("base"), view.get("live")
        base_text = display_text(_BASE_WORDS.get(base, base)) if isinstance(base, str) else "不明"
        live_text = display_text(_LIVE_WORDS.get(live, live)) if isinstance(live, str) else "不明"
        return (f"警告: この候補の土台は配備中の版と違います (土台 {base_text} / "
                f"配備中 {live_text})。このままでは配備できません")
    if state == base_identity.STATE_NONE_BUT_LIVE:
        return ("警告: この候補は同名の配備中の版を見ずに作られました。"
                "このままでは配備できません")
    if state == base_identity.STATE_UNREADABLE:
        return "警告: 配備中の版を確かめられません"
    return None


def lines(view) -> list[str]:
    line = warning_line(view)
    return [line] if line is not None else []

"""承認待ちの依頼の「土台」項目の固定文言 (web と承認カードが共有する)。

材料は daemon の承認詳細の `base_check` (`{"state", "base", "live"}`)。文言の正本は daemon 側
(`agentic_fx/ops/base_view.py` と `agentic_fx/plugin/base_identity.py`) で、afx-ops は本体を
import しないので写しを持つ。両 package の文言一致は tests/ops の 2 package 検査が pin する。
"""
from __future__ import annotations

import re

BASE_CHANGED = "base_changed"
BASE_CHANGED_TEXT = ("土台の版が配備中の版と違うため配備しませんでした。"
                     "配備中の版から作り直して提出してください")

STATE_MISMATCH = "mismatch"
STATE_NONE_BUT_LIVE = "none_but_live"
STATE_UNREADABLE = "unreadable"

_HEX12 = re.compile(r"[0-9a-f]{12}\Z")
_WORDS = {"none": "無し", "plain": "plain", "unreadable": "読めない", "unknown": "不明"}


def _shown(value: object) -> str:
    """土台 / 配備中の表示。12 桁 hex と既知の語だけを通し、それ以外は「不明」。"""
    if type(value) is not str:
        return "不明"
    if _HEX12.match(value):
        return value
    return _WORDS.get(value, "不明")


def warning_line(view: object) -> str | None:
    """警告が要る状態のときだけ 1 行の文を返す。壊れた形・一致・旧形式・bless は None。"""
    if type(view) is not dict:
        return None
    state = view.get("state")
    if state == STATE_MISMATCH:
        return (f"警告: この候補の土台は配備中の版と違います (土台 {_shown(view.get('base'))} / "
                f"配備中 {_shown(view.get('live'))})。このままでは配備できません")
    if state == STATE_NONE_BUT_LIVE:
        return ("警告: この候補は同名の配備中の版を見ずに作られました。"
                "このままでは配備できません")
    if state == STATE_UNREADABLE:
        return "警告: 配備中の版を確かめられません"
    return None

"""改善 mission の prompt が `get_signals` の不在を設計上の制約として伝え、
改善 registry の tool 集合と禁止集合を変えないことの pin。"""
from __future__ import annotations

import re

from agentic_fx.loops.improve_context import mission_template
from agentic_fx.tools import signal_tools

_CANONICAL_NOTICE = (
    "`get_signals` は取引判断 loop 専用で、この改善 mission では利用できません。"
    "配備済み strategy の直近 signal の方向・status・payload は、仮説の選択や"
    "候補評価の材料にしないでください。この不在は設計上の制約なので、課題・"
    "note・discovery として記録せず、候補自体は許可された `run_backtest` と"
    "self-test で検証してください。過去の note にある signal 照合や"
    "`get_signals` 相当の tool の要望は、この制約によって解消済みです。"
    "それに従って tool を探したり、同じ要望を再記録したりしないでください。"
)


def _rule_text(section: str, number: int) -> str:
    """規律 <number> の本文を、番号・太字記号・行頭 indent・改行を除いた
    1 本の文字列にする。"""
    m = re.search(rf"^{number}\. (.*?)(?=^\d+\. |\Z)", section, re.M | re.S)
    assert m, f"規律 {number} が無い"
    body = m.group(1)
    lines = [ln.strip() for ln in body.splitlines()]
    return "".join(lines).replace("**", "")


def _discipline_section(template: str) -> str:
    start = template.index("## 規律 (必ず守ること)")
    end = template.index("\n## ", start + 1)
    return template[start:end]


def test_the_discipline_section_states_that_get_signals_is_unavailable_by_design():
    section = _discipline_section(mission_template())
    # 1 つの番号付き規律として、他の規律と同じ形で置かれている。
    assert re.search(r"^8\. \*\*`get_signals` は取引判断 loop 専用", section, re.M)
    # 空白と改行を除いた全文が §4.2 の基準文面と一致する (部分削除も red)。
    assert _rule_text(section, 8) == _CANONICAL_NOTICE


def test_the_notice_names_no_replacement_tool():
    """案 (b) の方向専用 tool は非目標。文面が代替 tool を示唆しない。"""
    section = _discipline_section(mission_template())
    assert "get_deployed_directions" not in section
    assert "方向専用" not in section


def test_improve_forbidden_set_is_unchanged_by_the_notice():
    assert signal_tools.IMPROVE_FORBIDDEN == frozenset({"get_signals", "bless"})

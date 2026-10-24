"""改善 mission の prompt が `get_signals` の不在を設計上の制約として伝え、
改善 registry の tool 集合と禁止集合を変えないことの pin。"""
from __future__ import annotations

import re

from agentic_fx.loops.improve_context import mission_template
from agentic_fx.tools import signal_tools

_REQUIRED_SENTENCES = (
    "`get_signals` は取引判断 loop 専用で、この改善 mission では利用できません。",
    "配備済み strategy の直近 signal の方向・status・payload は、仮説の選択や",
    "課題・",
    "note・discovery として記録せず、候補自体は許可された `run_backtest` と",
    "過去の note にある signal 照合や",
    "`get_signals` 相当の tool の要望は、この制約によって解消済みです。",
    "それに従って tool を探したり、同じ要望を再記録したりしないでください。",
)


def _discipline_section(template: str) -> str:
    start = template.index("## 規律 (必ず守ること)")
    end = template.index("\n## ", start + 1)
    return template[start:end]


def test_the_discipline_section_states_that_get_signals_is_unavailable_by_design():
    section = _discipline_section(mission_template())
    for sentence in _REQUIRED_SENTENCES:
        assert sentence in section, sentence
    # 1 つの番号付き規律として、他の規律と同じ形で置かれている。
    assert re.search(r"^8\. \*\*`get_signals` は取引判断 loop 専用", section, re.M)


def test_the_notice_names_no_replacement_tool():
    """案 (b) の方向専用 tool は非目標。文面が代替 tool を示唆しない。"""
    section = _discipline_section(mission_template())
    assert "get_deployed_directions" not in section
    assert "方向専用" not in section


def test_improve_forbidden_set_is_unchanged_by_the_notice():
    assert signal_tools.IMPROVE_FORBIDDEN == frozenset({"get_signals", "bless"})

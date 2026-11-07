"""get_econ_reactions: past post-release price-move statistics for the cron trade mission."""
from __future__ import annotations

import sqlite3
from typing import Mapping

from agentic_fx.analytics.econ_reactions import AGENT_FLOOR, agent_view
from agentic_fx.config import Settings
from agentic_fx.store.econ_reaction_values import bundle_stats
from agentic_fx.tools.registry import ToolDef

DESCRIPTION = (
    "過去の指標発表後の値動き幅の統計。予測ではない。床 20 件未満は数値を出さない "
    "(status=insufficient)。数値はすべて pips、窓は発表後 15 / 60 / 240 分。"
    "pair の base / quote 通貨それぞれの束 (通貨 × 指標の種別 × 窓) を返す")


def build(conn: sqlite3.Connection, settings: Settings,
          pair_currencies: Mapping[str, tuple[str, str]]) -> list[ToolDef]:
    pairs = list(settings.pairs)

    def get_econ_reactions(pair: str) -> list[dict]:
        if pair not in pairs:
            raise ValueError(f"pair must be one of {pairs}: {pair!r}")
        return agent_view(bundle_stats(conn), pair=pair,
                          pair_currencies=pair_currencies, floor=AGENT_FLOOR)

    return [ToolDef(
        "get_econ_reactions", DESCRIPTION,
        {"type": "object",
         "properties": {"pair": {"enum": pairs, "description": "e.g. USDJPY"}},
         "required": ["pair"]},
        get_econ_reactions)]

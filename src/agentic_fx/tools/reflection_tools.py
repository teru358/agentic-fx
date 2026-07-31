from __future__ import annotations

import sqlite3

from agentic_fx.store import reflections
from agentic_fx.store.rag import Rag
from agentic_fx.tools.registry import ToolDef


def build(conn: sqlite3.Connection, rag: Rag, pairs: list[str]) -> list[ToolDef]:
    def get_recent_reflections(pair: str, n: int = 5) -> list[dict]:
        return reflections.recent_for_pair(conn, pair, n)

    def search_reflections(query: str) -> list[dict]:
        return rag.search_reflections(query, n=5)

    # Build pair schema with enum constraint (pairs is required)
    pair_schema = {"enum": list(pairs)}

    return [
        ToolDef("get_recent_reflections",
                "指定ペアの直近トレード振り返り (スペック §5)",
                {"type": "object",
                 "properties": {"pair": pair_schema,
                                "n": {"type": "integer", "minimum": 1,
                                      "maximum": 20}},
                 "required": ["pair"]}, get_recent_reflections),
        ToolDef("search_reflections", "過去の振り返りの意味検索 (類似局面)",
                {"type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"]}, search_reflections),
    ]

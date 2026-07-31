from __future__ import annotations

from agentic_fx.store.rag import Rag
from agentic_fx.tools.registry import ToolDef


def build(rag: Rag) -> list[ToolDef]:
    def search_news(query: str) -> list[dict]:
        return rag.search_news(query, n=5)

    return [ToolDef("search_news", "収集済みニュースの意味検索 (上位 5 件)",
                    {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}, search_news)]

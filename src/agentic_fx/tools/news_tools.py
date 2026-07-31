from __future__ import annotations

from agentic_fx.store.rag import Rag
from agentic_fx.tools.registry import ToolDef


def build(rag: Rag) -> list[ToolDef]:
    def search_news(query: str) -> list[dict]:
        # Project to safe fields only (drop url which may contain credentials).
        # LLM has no fetch tool, so URL is unusable anyway.
        results = rag.search_news(query, n=5)
        return [{"title": r["title"], "body": r["body"], "source_name": r["source_name"]}
                for r in results]

    return [ToolDef("search_news", "収集済みニュースの意味検索 (上位 5 件)",
                    {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}, search_news)]

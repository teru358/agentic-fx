from __future__ import annotations

from agentic_fx.store.rag import Rag
from agentic_fx.tools.registry import ToolDef


def build(rag: Rag) -> list[ToolDef]:
    def search_news(query: str) -> list[dict]:
        # Project to safe fields only (drop url which may contain credentials).
        # LLM has no fetch tool, so URL is unusable anyway.
        # Use .get() to gracefully handle missing metadata fields (source_name, etc)
        results = rag.search_news(query, n=5)
        # ingested_at は初回取込み時刻 (記事の発表時刻ではない)。
        return [{"title": r.get("title"), "body": r.get("body"),
                 "source_name": r.get("source_name"),
                 "ingested_at": r.get("added_at")}
                for r in results]

    return [ToolDef("search_news", "収集済みニュースの意味検索 (上位 5 件)",
                    {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}, search_news)]

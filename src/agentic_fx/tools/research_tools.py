"""improve registry の研究ツール — web_search / fetch_article (設計書 §3.4/§6)。

advisory 予算 (§6): 安全保証ではなくツール契約。改善 profile は shell/python
を持つため、この予算は agent が `web_search`/`fetch_article` を使う場合にのみ
数えられる。予算超過・レート制御は Mission (= プロセス) 内のクロージャ状態で
足りる (Mission ごとに worker プロセスが 1 つ、§3.1)。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol
from urllib.parse import urlparse

from agentic_fx.tools.registry import ToolDef

if TYPE_CHECKING:
    from agentic_fx.config import ResearchSettings


class _SearchBackend(Protocol):
    def text(self, query: str, max_results: int) -> list[dict]: ...


class _FetchBackend(Protocol):
    def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]: ...


def _default_search_backend() -> _SearchBackend:
    from ddgs import DDGS
    return DDGS()


def _default_fetch_backend() -> _FetchBackend:
    class _TrafilaturaBackend:
        def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]:
            import trafilatura
            downloaded = trafilatura.fetch_url(url)
            if downloaded is None:
                return 599, ""
            text = trafilatura.extract(downloaded) or ""
            return 200, text
    return _TrafilaturaBackend()


def build_research_tooldefs(
        *, settings: "ResearchSettings",
        search_backend: _SearchBackend | None = None,
        fetch_backend: _FetchBackend | None = None,
        monotonic: Callable[[], float] | None = None) -> list[ToolDef]:
    import time as _time
    search_backend = search_backend or _default_search_backend()
    fetch_backend = fetch_backend or _default_fetch_backend()
    monotonic = monotonic or _time.monotonic

    state = {
        "search_count": 0, "fetch_count": 0,
        "last_search_at": None,
        "per_host_count": {},        # host -> int
        "aborted_hosts": set(),      # 429/503 を受けた host
    }

    def web_search(query: str, max_results: int) -> dict:
        if state["search_count"] >= settings.max_searches:
            return {"error": "budget exhausted"}
        now = monotonic()
        last = state["last_search_at"]
        if last is not None and (now - last) < settings.min_interval_sec:
            return {"error": "budget exhausted"}
        state["search_count"] += 1
        state["last_search_at"] = now
        results = search_backend.text(query, max_results)
        return {"results": results}

    def fetch_article(url: str) -> dict:
        host = urlparse(url).netloc
        if host in state["aborted_hosts"]:
            return {"error": "host aborted (429/503)"}
        if state["fetch_count"] >= settings.max_fetches:
            return {"error": "budget exhausted"}
        if state["per_host_count"].get(host, 0) >= settings.max_per_host:
            return {"error": "budget exhausted"}
        state["fetch_count"] += 1
        state["per_host_count"][host] = state["per_host_count"].get(host, 0) + 1
        status, text = fetch_backend.fetch(url, user_agent=settings.user_agent)
        if status in (429, 503):
            state["aborted_hosts"].add(host)
            return {"error": f"fetch failed: {status}"}
        if status != 200:
            return {"error": f"fetch failed: {status}"}
        max_bytes = settings.fetch_max_bytes
        encoded = text.encode("utf-8")
        if len(encoded) > max_bytes:
            text = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return {"text": text}

    return [
        ToolDef(name="web_search",
                description="DuckDuckGo で検索する (advisory 予算あり)。",
                parameters={"type": "object",
                            "properties": {"query": {"type": "string"},
                                          "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
                            "required": ["query", "max_results"]},
                func=web_search),
        ToolDef(name="fetch_article",
                description="記事本文を抽出する (advisory 予算あり)。",
                parameters={"type": "object",
                            "properties": {"url": {"type": "string"}},
                            "required": ["url"]},
                func=fetch_article),
    ]

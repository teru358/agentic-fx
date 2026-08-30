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
    def text(self, query: str, *, max_results: int) -> list[dict]: ...


class _FetchBackend(Protocol):
    def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]: ...


def _default_search_backend() -> _SearchBackend:
    from ddgs import DDGS
    return DDGS()


def _default_fetch_backend() -> _FetchBackend:
    class _TrafilaturaBackend:
        def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]:
            import trafilatura
            from trafilatura.settings import use_config

            # B3 (検収 Blocking): trafilatura.fetch_url に user_agent を渡す
            # 直接の引数は無い — `config` (ConfigParser) の `USER_AGENTS`
            # (`DEFAULT` セクション、改行区切りで複数可、`_determine_headers`
            # が `random.choice` する) を経由するのが実装済みの面 (実 API を
            # 確認して選定、trafilatura==2.1.0)。素性を名乗る UA を実際に
            # 送るため、`settings.user_agent` を単独候補として設定する。
            config = use_config()
            if user_agent:
                config.set("DEFAULT", "USER_AGENTS", user_agent)
            downloaded = trafilatura.fetch_url(url, config=config)
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
        # M-1 (検収 Minor): §6 は 6 軸を web_search/fetch_article の
        # Mission 予算としてまとめて列挙している — min_interval_sec は
        # search 専用ではないので fetch 側にも独立のゲートを持たせる。
        "last_fetch_at": None,
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
        results = search_backend.text(query, max_results=max_results)
        return {"results": results}

    def fetch_article(url: str) -> dict:
        # L01: `netloc` の生値 (大文字小文字・port・userinfo を含む) を
        # そのまま host キーにすると `max_per_host`/429 遮断を表記違いで
        # 回避できる ([[outbound-request-budget-is-a-design-constraint]])。
        # `hostname` (userinfo/port 除去済み) を小文字化して正規化する。
        # 副作用は意図的: port 違いは同一 host として合算される。
        host = (urlparse(url).hostname or "").lower()
        if host in state["aborted_hosts"]:
            return {"error": "host aborted (429/503)"}
        if state["fetch_count"] >= settings.max_fetches:
            return {"error": "budget exhausted"}
        if state["per_host_count"].get(host, 0) >= settings.max_per_host:
            return {"error": "budget exhausted"}
        now = monotonic()
        last = state["last_fetch_at"]
        if last is not None and (now - last) < settings.min_interval_sec:
            return {"error": "budget exhausted"}
        state["fetch_count"] += 1
        state["per_host_count"][host] = state["per_host_count"].get(host, 0) + 1
        state["last_fetch_at"] = now
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

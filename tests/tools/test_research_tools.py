"""improve registry の研究ツール — advisory 予算のツール契約テスト
(設計書 §6、§8.1-14)。予算は安全保証ではなくツール契約 — 変異は
「21 回目が通る」「429 で再試行する」等の**カウンタの穴**だけを狙う。"""
from __future__ import annotations

import json

import pytest

from agentic_fx.config import ResearchSettings
from agentic_fx.tools.research_tools import build_research_tooldefs


def _settings(**overrides) -> "ResearchSettings":
    base = dict(max_searches=20, max_fetches=30, min_interval_sec=2.0,
                max_per_host=5, fetch_max_bytes=2_097_152,
                user_agent="agentic-fx/test (+https://example/test)")
    base.update(overrides)
    return ResearchSettings(**base)


class _FakeSearchBackend:
    """ddgs.DDGS 相当の最小 fake。`calls` に呼び出し引数を記録する。"""

    def __init__(self, results: list[dict] | None = None):
        self.calls: list[dict] = []
        self._results = results if results is not None else [
            {"title": "t", "href": "https://example.com/a", "body": "b"}]

    def text(self, query, max_results):
        self.calls.append({"query": query, "max_results": max_results})
        return list(self._results)


class _FakeFetchBackend:
    """trafilatura 相当の最小 fake。ホストごとに status を返せる。"""

    def __init__(self, status_by_call: list[int] | None = None,
                 body: str = "article body"):
        self.calls: list[str] = []
        self._status_by_call = status_by_call or []
        self._body = body

    def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]:
        self.calls.append(url)
        idx = len(self.calls) - 1
        status = (self._status_by_call[idx]
                  if idx < len(self._status_by_call) else 200)
        return status, self._body


def _build(settings=None, search_backend=None, fetch_backend=None, clock=None):
    settings = settings or _settings()
    search_backend = search_backend or _FakeSearchBackend()
    fetch_backend = fetch_backend or _FakeFetchBackend()
    ticks = iter(clock or (float(i) for i in range(0, 100000, 1)))
    tools = build_research_tooldefs(
        settings=settings, search_backend=search_backend,
        fetch_backend=fetch_backend, monotonic=lambda: next(ticks))
    return {t.name: t for t in tools}, search_backend, fetch_backend


def test_web_search_budget_max_searches_exhausted_returns_error_not_raise():
    """軸: 件数上限。21 回目は search_backend を呼ばず error を返す。"""
    tools, backend, _ = _build(_settings(max_searches=2, min_interval_sec=0.01))
    for _ in range(2):
        out = tools["web_search"].func(query="q", max_results=3)
        assert "error" not in out
    out3 = tools["web_search"].func(query="q", max_results=3)
    assert out3 == {"error": "budget exhausted"}
    assert len(backend.calls) == 2  # 3 回目は backend に到達しない


def test_fetch_article_budget_max_fetches_exhausted_returns_error_not_raise():
    """軸: 件数上限 (fetch 側は別カウンタ)。"""
    tools, _, backend = _build(_settings(max_fetches=1, min_interval_sec=0.01))
    ok = tools["fetch_article"].func(url="https://a.example/1")
    assert "error" not in ok
    exhausted = tools["fetch_article"].func(url="https://a.example/2")
    assert exhausted == {"error": "budget exhausted"}
    assert backend.calls == ["https://a.example/1"]


def test_web_search_min_interval_sec_enforced():
    """軸: 最小間隔。間隔未満の 2 回目は backend を呼ばず error。"""
    clock_values = iter([0.0, 0.5])  # 0.5 < min_interval_sec=2.0
    tools, backend, _ = _build(
        _settings(min_interval_sec=2.0, max_searches=10),
        clock=clock_values)
    out1 = tools["web_search"].func(query="q", max_results=1)
    assert "error" not in out1
    out2 = tools["web_search"].func(query="q", max_results=1)
    assert out2 == {"error": "budget exhausted"}
    assert len(backend.calls) == 1


def test_fetch_article_min_interval_sec_enforced():
    """M-1 (検収 Minor): 間隔ゲートは web_search 専用ではない — §6 は
    6 軸を web_search/fetch_article の Mission 予算としてまとめて列挙する。
    間隔未満の 2 回目の fetch は backend を呼ばず error。"""
    clock_values = iter([0.0, 0.5])  # 0.5 < min_interval_sec=2.0
    tools, _, backend = _build(
        _settings(min_interval_sec=2.0, max_fetches=10),
        clock=clock_values)
    out1 = tools["fetch_article"].func(url="https://a.example/1")
    assert "error" not in out1
    out2 = tools["fetch_article"].func(url="https://a.example/2")
    assert out2 == {"error": "budget exhausted"}
    assert len(backend.calls) == 1


def test_fetch_article_max_per_host_enforced():
    """軸: host 上限。同一 host への 6 回目 (上限 5) は error。"""
    tools, _, backend = _build(
        _settings(max_per_host=5, max_fetches=100, min_interval_sec=0.01))
    for i in range(5):
        out = tools["fetch_article"].func(
            url=f"https://same.example/{i}")
        assert "error" not in out
    out6 = tools["fetch_article"].func(url="https://same.example/6")
    assert out6 == {"error": "budget exhausted"}
    assert len(backend.calls) == 5
    # 別 host は独立カウンタ — 上限に影響しない
    other = tools["fetch_article"].func(url="https://other.example/1")
    assert "error" not in other


def test_fetch_article_sends_configured_user_agent():
    """軸: UA。settings.user_agent が backend へ渡ること
    (裁定 4: 素性を名乗る UA)。"""
    seen_ua: list[str] = []

    class _UACapturingBackend(_FakeFetchBackend):
        def fetch(self, url, *, user_agent=None):
            seen_ua.append(user_agent)
            return super().fetch(url)

    tools, _, _ = _build(
        _settings(user_agent="agentic-fx/9.9 (+https://x/y)",
                  min_interval_sec=0.01),
        fetch_backend=_UACapturingBackend())
    tools["fetch_article"].func(url="https://a.example/1")
    assert seen_ua == ["agentic-fx/9.9 (+https://x/y)"]


def test_default_fetch_backend_actually_sends_configured_user_agent(monkeypatch):
    """B3 (検収 Blocking): `test_fetch_article_sends_configured_user_agent`
    は fake backend に対して「呼び出し側が UA を渡したか」しか見ておらず、
    本番 `_default_fetch_backend()` (trafilatura) が実際に UA を運ぶかは
    検査していなかった (モックが潰した次元)。

    ここでは fake ではなく本番 `_default_fetch_backend()` の構築物を使う —
    実ネットワークに出さないため `trafilatura.fetch_url` だけを monkeypatch
    し、そこに渡された `config` (ConfigParser) から実際に導出されるヘッダ
    (`trafilatura.downloads._determine_headers`) に settings の UA 文字列が
    現れることを見る。"""
    import trafilatura
    from trafilatura.downloads import _determine_headers

    from agentic_fx.tools.research_tools import _default_fetch_backend

    captured: dict = {}

    def _fake_fetch_url(url, config=None, **kwargs):
        captured["config"] = config
        return None

    monkeypatch.setattr(trafilatura, "fetch_url", _fake_fetch_url)

    backend = _default_fetch_backend()
    backend.fetch("https://example.invalid/a",
                  user_agent="agentic-fx/9.9 (+https://x/y)")

    assert "config" in captured, "trafilatura.fetch_url に config が渡っていない"
    headers = _determine_headers(captured["config"])
    assert headers["User-Agent"] == "agentic-fx/9.9 (+https://x/y)"


def test_fetch_article_429_aborts_host_for_rest_of_mission():
    """軸: 429/503 即中止。同一 Mission (= 同一プロセス、同一 backend
    インスタンス) 内で同一 host への以後の呼び出しは backend に到達せず
    error を返す (再試行しない)。"""
    tools, _, backend = _build(
        _settings(min_interval_sec=0.01, max_fetches=100),
        fetch_backend=_FakeFetchBackend(status_by_call=[429]))
    first = tools["fetch_article"].func(url="https://x.example/1")
    assert first == {"error": "fetch failed: 429"}
    second = tools["fetch_article"].func(url="https://x.example/2")
    assert second == {"error": "host aborted (429/503)"}
    assert len(backend.calls) == 1  # 2 回目は backend に到達しない


def test_fetch_article_503_aborts_host_for_rest_of_mission():
    """軸: 429/503 即中止 (503 も同じ規律)。"""
    tools, _, backend = _build(
        _settings(min_interval_sec=0.01, max_fetches=100),
        fetch_backend=_FakeFetchBackend(status_by_call=[503]))
    tools["fetch_article"].func(url="https://y.example/1")
    second = tools["fetch_article"].func(url="https://y.example/2")
    assert second == {"error": "host aborted (429/503)"}
    assert len(backend.calls) == 1


def test_fetch_article_truncates_at_fetch_max_bytes():
    """軸: サイズ上限。fetch_max_bytes を超える本文は切り詰められる
    (§3.4 fetch_article のサイズ上限)。"""
    long_body = "x" * 100
    tools, _, _ = _build(
        _settings(fetch_max_bytes=10, min_interval_sec=0.01),
        fetch_backend=_FakeFetchBackend(body=long_body))
    out = tools["fetch_article"].func(url="https://a.example/1")
    assert len(out["text"].encode("utf-8")) <= 10

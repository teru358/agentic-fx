"""improve registry の研究ツール — advisory 予算のツール契約テスト
(設計書 §6、§8.1-14)。予算は安全保証ではなくツール契約 — 変異は
「21 回目が通る」「429 で再試行する」等の**カウンタの穴**だけを狙う。"""
from __future__ import annotations

import json

import pytest

from agentic_fx.config import ResearchSettings
from agentic_fx.tools.registry import ToolRegistry
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


def test_web_search_min_interval_sec_boundary_equal_is_allowed():
    """L13: `now - last == min_interval_sec` ちょうどの境界が未検証
    (既存は 0.5 vs 2.0)。`<` → `<=` 変異が生存する — 境界ちょうどでは
    成功することを pin する。"""
    clock_values = iter([0.0, 2.0])  # delta == min_interval_sec ちょうど
    tools, backend, _ = _build(
        _settings(min_interval_sec=2.0, max_searches=10),
        clock=clock_values)
    out1 = tools["web_search"].func(query="q", max_results=1)
    assert "error" not in out1
    out2 = tools["web_search"].func(query="q", max_results=1)
    assert "error" not in out2
    assert len(backend.calls) == 2


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


def test_fetch_article_429_abort_is_host_independent():
    """L14: 429 遮断の host 独立性が未検証 (既存は単一 host のみ)。
    `aborted_hosts` を bool フラグにする変異が生存する — 別 host への
    呼び出しは影響を受けないことを確認する。"""
    tools, _, backend = _build(
        _settings(min_interval_sec=0.01, max_fetches=100),
        fetch_backend=_FakeFetchBackend(status_by_call=[429, 200]))
    first = tools["fetch_article"].func(url="https://blocked.example/1")
    assert first == {"error": "fetch failed: 429"}
    second = tools["fetch_article"].func(url="https://other.example/1")
    assert second == {"text": "article body"}
    assert len(backend.calls) == 2


def test_fetch_article_503_aborts_host_for_rest_of_mission():
    """軸: 429/503 即中止 (503 も同じ規律)。"""
    tools, _, backend = _build(
        _settings(min_interval_sec=0.01, max_fetches=100),
        fetch_backend=_FakeFetchBackend(status_by_call=[503]))
    tools["fetch_article"].func(url="https://y.example/1")
    second = tools["fetch_article"].func(url="https://y.example/2")
    assert second == {"error": "host aborted (429/503)"}
    assert len(backend.calls) == 1


def test_web_search_max_results_clamp_enforced_via_registry_execute():
    """M02 (段 0 Important): `max_results` の schema `maximum: 20` は
    飾りではなく `ToolRegistry.execute` の `jsonschema.validate` が実行経路
    上で効かせている実効ガード。`ToolDef.func` を直接呼ぶ既存テストは
    この門を一切通らないため、ここでは registry 越しに呼ぶ。"""
    backend = _FakeSearchBackend()
    tools, _, _ = _build(_settings(min_interval_sec=0.01), search_backend=backend)
    registry = ToolRegistry()
    registry.register_all(list(tools.values()))
    out = registry.execute(
        "web_search", {"query": "q", "max_results": 21}, ["web_search"])
    assert "invalid arguments" in out
    assert backend.calls == []


class _AlwaysRaisingSearchBackend:
    """L02 killer: `web_search` 側にも段 0 M01 と同型の pin を足す。
    呼ばれるたびに例外を投げ、予算カウンタが backend 呼び出しの前に
    加算されることを確認する。"""

    def __init__(self):
        self.calls = 0

    def text(self, query: str, max_results: int) -> list[dict]:
        self.calls += 1
        raise ConnectionError("boom")


def test_web_search_max_searches_budget_consumed_even_when_backend_raises():
    backend = _AlwaysRaisingSearchBackend()
    tools, _, _ = _build(
        _settings(max_searches=2, min_interval_sec=0.01), search_backend=backend)
    for i in range(2):
        with pytest.raises(ConnectionError):
            tools["web_search"].func(query=f"q{i}", max_results=1)
    assert backend.calls == 2
    out = tools["web_search"].func(query="q3", max_results=1)
    assert out == {"error": "budget exhausted"}
    assert backend.calls == 2


class _AlwaysCountingFetchBackend:
    """L01: 200 を返し続けて到達回数だけ数える fake。"""

    def __init__(self):
        self.calls = 0

    def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]:
        self.calls += 1
        return 200, "body"


class _AlwaysRaisingFetchBackend:
    """M01 (段 0 致命): 呼ばれるたびに例外を投げる backend。予算カウンタが
    backend 呼び出しの**前**に加算されることの pin — 後に加算する変異では
    例外側の分岐で加算がスキップされ、予算が無制限になる。"""

    def __init__(self):
        self.calls: list[str] = []

    def fetch(self, url: str, *, user_agent: str | None = None) -> tuple[int, str]:
        self.calls.append(url)
        raise ConnectionError("boom")


def test_fetch_article_max_fetches_budget_consumed_even_when_backend_raises():
    """M01 killer: 例外を投げる backend に対しても max_fetches は消費される
    — 2 回目 (max_fetches=2) までは backend に到達 (して例外が伝播せず
    `ToolRegistry.execute` 相当の握り潰しは無いのでここでは例外がそのまま
    伝播することを確認しつつ)、3 回目以降は backend に到達せず
    `budget exhausted` を返す。"""
    backend = _AlwaysRaisingFetchBackend()
    tools, _, _ = _build(
        _settings(max_fetches=2, min_interval_sec=0.01), fetch_backend=backend)
    for i in range(2):
        with pytest.raises(ConnectionError):
            tools["fetch_article"].func(url=f"https://a.example/{i}")
    assert len(backend.calls) == 2
    out = tools["fetch_article"].func(url="https://a.example/3rd")
    assert out == {"error": "budget exhausted"}
    assert len(backend.calls) == 2  # 3 回目は backend に到達しない


def test_fetch_article_max_per_host_budget_consumed_even_when_backend_raises():
    """M01 killer (host 軸): 同一 host への呼び出しが例外を投げても
    per_host_count は消費され、上限超過後は backend に到達しない。"""
    backend = _AlwaysRaisingFetchBackend()
    tools, _, _ = _build(
        _settings(max_per_host=2, max_fetches=100, min_interval_sec=0.01),
        fetch_backend=backend)
    for i in range(2):
        with pytest.raises(ConnectionError):
            tools["fetch_article"].func(url=f"https://same.example/{i}")
    assert len(backend.calls) == 2
    out = tools["fetch_article"].func(url="https://same.example/3rd")
    assert out == {"error": "budget exhausted"}
    assert len(backend.calls) == 2


def test_fetch_article_truncates_at_fetch_max_bytes():
    """軸: サイズ上限。fetch_max_bytes を超える本文は切り詰められる
    (§3.4 fetch_article のサイズ上限)。"""
    long_body = "x" * 100
    tools, _, _ = _build(
        _settings(fetch_max_bytes=10, min_interval_sec=0.01),
        fetch_backend=_FakeFetchBackend(body=long_body))
    out = tools["fetch_article"].func(url="https://a.example/1")
    assert len(out["text"].encode("utf-8")) <= 10


def test_fetch_article_non_2xx_non_429_503_status_is_error():
    """L15: `_FakeFetchBackend` が 200/429/503 以外を返さないため、
    `status != 200` を緩める変異 (例: `status not in (429, 503)`) が生存
    する。404 で明示的に error を pin する。"""
    tools, _, _ = _build(
        _settings(min_interval_sec=0.01),
        fetch_backend=_FakeFetchBackend(status_by_call=[404]))
    out = tools["fetch_article"].func(url="https://a.example/1")
    assert out == {"error": "fetch failed: 404"}


def test_fetch_article_truncates_multibyte_char_boundary():
    """L16: 切り詰め (`encoded[:max_bytes].decode(errors="ignore")`) の
    マルチバイト境界が未検証 (既存は ASCII のみ)。日本語本文で境界を
    またぐケースを確認する — 不正なバイト列にならず有効な UTF-8 文字列
    として返ることを見る。"""
    # "あ" は UTF-8 で 3 バイト。max_bytes=5 だと 1 文字目 (3B) は収まり、
    # 2 文字目 (3B) は境界をまたいで 2B しか入らない (不正シーケンス)。
    body = "あああ"
    tools, _, _ = _build(
        _settings(fetch_max_bytes=5, min_interval_sec=0.01),
        fetch_backend=_FakeFetchBackend(body=body))
    out = tools["fetch_article"].func(url="https://a.example/1")
    assert len(out["text"].encode("utf-8")) <= 5
    # errors="ignore" で不正末尾バイトが破棄され、有効な UTF-8 のみが残る
    assert out["text"].encode("utf-8").decode("utf-8") == out["text"]
    assert out["text"] == "あ"  # 3B のみ収まり、境界をまたぐ 2 文字目は破棄される


# --- L01: host キー正規化 (`max_per_host` / 429 遮断の表記違い回避) ---

def test_fetch_article_max_per_host_normalizes_case_port_and_userinfo():
    """L01 killer: `urlparse(url).netloc` の生値を host キーに使うと、
    大文字化・ポート付与・userinfo 付与のいずれでも別 host 扱いになり
    `max_per_host` を回避できる。host は
    `(urlparse(url).hostname or "").lower()` で正規化すること。"""
    backend = _AlwaysCountingFetchBackend()
    tools, _, _ = _build(
        _settings(max_per_host=2, max_fetches=100, min_interval_sec=0.01),
        fetch_backend=backend)
    urls = [
        "https://ex.example/1",
        "https://EX.example/2",
        "https://ex.example:443/3",
        "https://u@ex.example/4",
        "https://ex.example/5",
    ]
    outs = [tools["fetch_article"].func(url=u) for u in urls]
    assert backend.calls == 2, (
        f"host 正規化が効いていれば backend 到達は 2 回のみ (実測 {backend.calls})")
    assert outs[2:] == [{"error": "budget exhausted"}] * 3


def test_fetch_article_429_host_abort_survives_case_and_port_variants():
    """L01 killer (429 側): 429 で遮断された host を大文字表記・ポート付き
    で再訪しても backend に到達しないこと。"""
    backend = _FakeFetchBackend(status_by_call=[429])
    tools, _, _ = _build(
        _settings(min_interval_sec=0.01, max_fetches=100),
        fetch_backend=backend)
    first = tools["fetch_article"].func(url="https://z.example/1")
    assert first == {"error": "fetch failed: 429"}
    for u in ["https://Z.example/2", "https://z.example:443/3",
              "https://u@z.example/4"]:
        out = tools["fetch_article"].func(url=u)
        assert out == {"error": "host aborted (429/503)"}
    assert len(backend.calls) == 1


def test_fetch_article_max_per_host_normalizes_via_registry_execute():
    """L01 killer (実経路): `fetch_article` の schema には `format`/
    `pattern` が無く `ToolRegistry.execute` の jsonschema 検査は素通りする
    ため、正規化は関数本体で効いている必要がある。"""
    backend = _AlwaysCountingFetchBackend()
    tools, _, _ = _build(
        _settings(max_per_host=2, max_fetches=100, min_interval_sec=0.01),
        fetch_backend=backend)
    registry = ToolRegistry()
    registry.register_all(list(tools.values()))
    for u in ["https://r.example/1", "https://R.example/2",
              "https://r.example:443/3", "https://r.example/4"]:
        registry.execute("fetch_article", {"url": u}, ["fetch_article"])
    assert backend.calls == 2

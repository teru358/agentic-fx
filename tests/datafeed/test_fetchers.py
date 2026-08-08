import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import httpx
import pytest

from agentic_fx.datafeed.fetchers import (
    Article, FeedFetchError, _unrecognized_tz_abbrev, fetch_feed, fetch_web,
)

# 修正ラウンド 1: fetch_feed が parsed.bozo を見るようになったため、テスト
# 二重の MagicMock (parsed 側) には bozo=False を明示する必要がある。
# MagicMock は未設定属性を自動生成し、その値 (別の MagicMock) は bool() で
# 常に True になるため、明示しないと「健全なフィード」のつもりの fixture が
# 軒並み bozo=True 扱いになってしまう (このモジュールの既存テストが
# 実際にそれで壊れた — Task 7 修正ラウンド 1 の実測)。
#
# **書き忘れを構造的に防ぐため、parsed の二重は必ずこのヘルパーで作る**
# (再レビュー指摘: 素の MagicMock を直接書くと、書き忘れたテストが
# 意図より弱くなる — caplog を見ないテストでは無音で弱くなる)。
def _parsed(**kw) -> MagicMock:
    """feedparser.parse の戻り値の二重。bozo は既定で False (= 健全)。"""
    kw.setdefault("bozo", False)
    return MagicMock(**kw)


FEED_XML_PARSED = _parsed()
FEED_XML_PARSED.entries = [
    MagicMock(link="https://ex.com/a1", title="Dollar rallies",
              summary="USD up on CPI",
              published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0)),
    MagicMock(link="https://ex.com/a2", title="BOJ holds", summary="",
              published_parsed=None, updated_parsed=None),
]


def test_fetch_feed_maps_entries():
    with patch("feedparser.parse", return_value=FEED_XML_PARSED):
        arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert len(arts) == 2
    assert arts[0].title == "Dollar rallies"
    assert arts[0].url == "https://ex.com/a1"
    assert arts[0].body == "USD up on CPI"
    assert arts[0].source_name == "example"
    assert arts[1].body == ""
    assert arts[1].published is None


def test_fetch_feed_published_is_utc_from_gmt_normalized_tuple():
    # feedparser は published_parsed を GMT 正規化済み 9-tuple として渡す
    # (rfc822.py: _parse_date_rfc822 の docstring「a UTC time tuple」)。
    # naive 化・オフセット取り違え・秒の切り捨てのいずれでも落ちる値を検証する。
    with patch("feedparser.parse", return_value=FEED_XML_PARSED):
        arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert arts[0].published == datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    assert arts[0].published.utcoffset() == timedelta(0)


def test_fetch_feed_falls_back_to_updated_parsed_for_atom():
    # Atom は <published> が任意・<updated> が必須。published_parsed が
    # 無いフィードで日時を丸ごと失わないことを確認する。
    # 秒をあえて非 0 (45) にする: [:5] スライス (秒切り捨て) の回帰を
    # 検出するため (0 秒だと [:5]/[:6] が同じ結果になり検出できない)。
    entry = MagicMock(link="https://ex.com/a3", title="ECB speaks",
                       summary="", published_parsed=None,
                       updated_parsed=(2026, 7, 21, 9, 30, 45, 1, 202, 0))
    parsed = _parsed(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        arts = fetch_feed("https://ex.com/atom", "example", timeout_sec=10)
    assert arts[0].published == datetime(2026, 7, 21, 9, 30, 45,
                                         tzinfo=timezone.utc)


def test_fetch_feed_skips_entry_missing_link():
    # SimpleNamespace は MagicMock と違い未設定属性へのアクセスで
    # AttributeError を送出する (feedparser の FeedParserDict と同じ挙動)。
    good = SimpleNamespace(link="https://ex.com/a1", title="T1",
                           summary="s1", published_parsed=None)
    bad = SimpleNamespace(title="no link", summary="s2",
                          published_parsed=None)
    parsed = _parsed(entries=[good, bad])
    with patch("feedparser.parse", return_value=parsed):
        arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert len(arts) == 1
    assert arts[0].url == "https://ex.com/a1"


def test_fetch_feed_missing_title_defaults_to_empty_string():
    entry = SimpleNamespace(link="https://ex.com/a1", summary="s1",
                            published_parsed=None)
    parsed = _parsed(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert arts[0].title == ""


# ---- 修正ラウンド 1: 未知のタイムゾーン略号を無音にしない ------------------
#
# feedparser の rfc822/w3dtf パーサはどちらも未知の略号 (JST 等) を無音で
# offset=0 (UTC 扱い) にフォールバックする。この「無音の間違い」を検知できる
# ことを、まず `_unrecognized_tz_abbrev` の単体テストで、次に `fetch_feed` の
# 統合テスト (caplog) で確認する。

def test_unrecognized_tz_abbrev_flags_jst():
    assert _unrecognized_tz_abbrev(
        "Wed, 22 Jul 2026 10:00:00 JST") == "JST"


def test_unrecognized_tz_abbrev_allows_numeric_offset():
    assert _unrecognized_tz_abbrev("Wed, 22 Jul 2026 10:00:00 +0900") is None
    assert _unrecognized_tz_abbrev("2026-07-22T10:00:00+09:00") is None
    assert _unrecognized_tz_abbrev("2026-07-22T10:00:00Z") is None


def test_unrecognized_tz_abbrev_allows_known_abbreviation():
    # rfc822.timezone_names / w3dtf.timezonenames に載っている略号
    assert _unrecognized_tz_abbrev("Wed, 22 Jul 2026 10:00:00 GMT") is None
    assert _unrecognized_tz_abbrev("Wed, 22 Jul 2026 10:00:00 EST") is None


def test_unrecognized_tz_abbrev_allows_utc_literal():
    # "utc" は feedparser の辞書に literal キーとして無いが、フォールバック
    # 先 (offset 0) と定義上の値が一致するため無音の間違いが存在しない特例
    assert _unrecognized_tz_abbrev("Wed, 22 Jul 2026 10:00:00 UTC") is None


def test_unrecognized_tz_abbrev_none_when_undeterminable():
    # 誤検知よりも見逃しを選ぶ (要求 4): 判定できない入力では警告を出さない
    assert _unrecognized_tz_abbrev(None) is None
    assert _unrecognized_tz_abbrev("") is None
    assert _unrecognized_tz_abbrev("   ") is None
    assert _unrecognized_tz_abbrev("2026-07-22") is None       # 日付のみ
    assert _unrecognized_tz_abbrev(MagicMock()) is None        # str ですらない


def test_fetch_feed_warns_on_unrecognized_tz_abbreviation(caplog):
    entry = MagicMock(
        link="https://ex.com/jst", title="JST article", summary="",
        published_parsed=(2026, 7, 22, 1, 0, 0, 2, 203, 0),
        published="Wed, 22 Jul 2026 10:00:00 JST")
    parsed = _parsed(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    # 記事は捨てない (ニュースは fail-open、価格と違い欠損で取引を止めない)
    assert len(arts) == 1
    assert arts[0].published is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any("JST" in m and "example" in m for m in messages)


def test_fetch_feed_no_warning_for_numeric_offset(caplog):
    entry = MagicMock(
        link="https://ex.com/a", title="t", summary="",
        published_parsed=(2026, 7, 22, 1, 0, 0, 2, 203, 0),
        published="Wed, 22 Jul 2026 10:00:00 +0900")
    parsed = _parsed(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert caplog.records == []


def test_fetch_feed_no_warning_for_known_abbreviation(caplog):
    entry = MagicMock(
        link="https://ex.com/a", title="t", summary="",
        published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0),
        published="Wed, 22 Jul 2026 10:00:00 GMT")
    parsed = _parsed(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert caplog.records == []


def test_fetch_feed_no_warning_when_raw_date_missing(caplog):
    # SimpleNamespace で「raw な日付文字列が本当に無い」を再現する
    # (MagicMock は未設定属性を自動生成し str 以外の値を返すため、
    # 実装の isinstance ガードで別経路を通ってしまい、この経路を確認できない)。
    entry = SimpleNamespace(
        link="https://ex.com/a", title="t", summary="",
        published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0))
    parsed = _parsed(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert caplog.records == []
    assert arts[0].published is not None


def test_fetch_web_extracts_body():
    resp = MagicMock(text="<html>...</html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as get, \
         patch("trafilatura.extract", return_value="本文テキスト") as ext, \
         patch("trafilatura.extract_metadata") as meta:
        meta.return_value = MagicMock(title="記事タイトル")
        arts = fetch_web("https://ex.com/page", "example", timeout_sec=10)
    assert len(arts) == 1
    assert arts[0].body == "本文テキスト"
    assert arts[0].title == "記事タイトル"
    assert arts[0].url == "https://ex.com/page"
    assert arts[0].source_name == "example"
    assert arts[0].published is None
    get.assert_called_once_with("https://ex.com/page", timeout=10,
                                follow_redirects=True)
    # コメント欄が本文に混入しないこと (ブリーフの逐語コードからの意図的変更)
    assert ext.call_args.kwargs.get("include_comments") is False


def test_fetch_web_title_falls_back_to_url_when_no_metadata():
    resp = MagicMock(text="<html>...</html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp), \
         patch("trafilatura.extract", return_value="本文テキスト"), \
         patch("trafilatura.extract_metadata", return_value=None):
        arts = fetch_web("https://ex.com/page", "example", timeout_sec=10)
    assert arts[0].title == "https://ex.com/page"


def test_fetch_web_extract_failure_returns_empty():
    resp = MagicMock(text="<html></html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp), \
         patch("trafilatura.extract", return_value=None):
        assert fetch_web("https://ex.com/page", "example", timeout_sec=10) == []


def test_fetch_web_propagates_http_error():
    # 404 等はここで握りつぶさず送出する (呼び出し側/将来の集約層が
    # ソース単位の fail-open を担当する。本関数の責務ではない)。
    resp = MagicMock(text="")
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404))
    with patch("httpx.get", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            fetch_web("https://ex.com/page", "example", timeout_sec=10)


# ---- 修正ラウンド 1: bozo (死んだフィード) を無音にしない --------------------
#
# feedparser はネットワーク層の失敗を例外化せず bozo フラグ (+
# bozo_exception) に無音で吸収する。レビュー指摘: 到達不能ホストに対する
# 実測で bozo=True / entries=[] / 例外なし が確認されており、これを
# 素通しすると死んだソースが永久に「0 件」を返し続けるだけで技術ログにも
# activity にも何も残らない。

def test_fetch_feed_raises_when_bozo_and_no_entries():
    # 実 feedparser がネットワーク失敗時に bozo_exception へ入れるのは
    # urllib.error.URLError (再レビューが到達不能ホストで実測)。fixture を
    # 実物に合わせる。
    parsed = _parsed(bozo=True, entries=[],
                     bozo_exception=URLError("Connection refused"))
    response_mock = MagicMock()
    response_mock.content = b"<rss></rss>"
    response_mock.raise_for_status = MagicMock()
    with patch("agentic_fx.datafeed.fetchers.httpx.get", return_value=response_mock), \
         patch("feedparser.parse", return_value=parsed):
        with pytest.raises(FeedFetchError):
            fetch_feed("https://dead.example/rss", "deadsource", timeout_sec=10)


def test_fetch_feed_raise_message_has_no_url_or_raw_exception_text():
    # bozo_exception の str() に URL/ホスト名が乗ることがある想定
    # (urllib 由来のエラー等) — 型名のみ使い、生の str(e) も URL も
    # メッセージに出さない。**実物の URLError の str() は URL を含まない**
    # ため、ここでは URL を含む str() を持つ例外を意図的に使い、
    # 「型名しか出さない」構造そのものにピンを打つ。
    parsed = _parsed(
        bozo=True, entries=[],
        bozo_exception=URLError("Connection refused to dead.example:443"))
    response_mock = MagicMock()
    response_mock.content = b"<rss></rss>"
    response_mock.raise_for_status = MagicMock()
    with patch("agentic_fx.datafeed.fetchers.httpx.get", return_value=response_mock), \
         patch("feedparser.parse", return_value=parsed):
        with pytest.raises(FeedFetchError) as exc_info:
            fetch_feed("https://dead.example/rss", "deadsource", timeout_sec=10)
    msg = str(exc_info.value)
    assert "dead.example" not in msg
    assert "443" not in msg
    assert "URLError" in msg  # 型名は診断のため残る


def test_fetch_feed_warns_but_returns_partial_when_bozo_with_entries(caplog):
    entry = MagicMock(link="https://ex.com/a1", title="t", summary="",
                      published_parsed=None, updated_parsed=None)
    parsed = _parsed(bozo=True, entries=[entry],
                       bozo_exception=ValueError("malformed trailer"))
    response_mock = MagicMock()
    response_mock.content = b"<rss></rss>"
    response_mock.raise_for_status = MagicMock()
    with patch("agentic_fx.datafeed.fetchers.httpx.get", return_value=response_mock), \
         patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            arts = fetch_feed("https://ex.com/rss", "partialsource", timeout_sec=10)
    # 部分的なパースエラーでも取れた記事は返す (ニュースは fail-open)
    assert len(arts) == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("partialsource" in m for m in messages)
    # 失敗理由 (bozo_exception の型名) も残ること。これが無いと
    # 「失敗は技術ログに残す」が例外側にしかピンが無く片肺になる
    # (再レビューの変異テストで reason を落としても全緑だった)。
    assert any("ValueError" in m for m in messages)
    assert "ex.com" not in " ".join(messages)  # URL を出さない
    # 生の str(e) は出さない (型名のみ)
    assert "malformed trailer" not in " ".join(messages)


def test_fetch_feed_no_bozo_warning_when_bozo_false(caplog):
    # 非退行: bozo=False (健全なフィードがたまたま 0 件) では警告も
    # 例外も出さない
    parsed = _parsed(entries=[])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            arts = fetch_feed("https://ex.com/rss", "example", timeout_sec=10)
    assert arts == []
    assert caplog.records == []


def test_fetch_web_uses_injected_timeout(monkeypatch):
    captured = {}

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        raise RuntimeError("stop here — this test only checks the timeout arg")

    import agentic_fx.datafeed.fetchers as fetchers_mod
    monkeypatch.setattr(fetchers_mod.httpx, "get", fake_get)
    with pytest.raises(RuntimeError):
        fetchers_mod.fetch_web("http://x", "s", timeout_sec=7.5)
    assert captured["timeout"] == 7.5


def test_fetch_feed_uses_httpx_with_injected_timeout(monkeypatch):
    """feedparser.parse(url) から httpx 経由の取得に変わったことの確認。"""
    captured = {}

    class FakeResponse:
        content = b"<rss><channel></channel></rss>"

        def raise_for_status(self):
            pass

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        return FakeResponse()

    import agentic_fx.datafeed.fetchers as fetchers_mod
    monkeypatch.setattr(fetchers_mod.httpx, "get", fake_get)
    fetchers_mod.fetch_feed("http://x", "s", timeout_sec=7.5)
    assert captured["timeout"] == 7.5

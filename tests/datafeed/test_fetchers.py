import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentic_fx.datafeed.fetchers import (
    Article, _unrecognized_tz_abbrev, fetch_feed, fetch_web,
)

FEED_XML_PARSED = MagicMock()
FEED_XML_PARSED.entries = [
    MagicMock(link="https://ex.com/a1", title="Dollar rallies",
              summary="USD up on CPI",
              published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0)),
    MagicMock(link="https://ex.com/a2", title="BOJ holds", summary="",
              published_parsed=None, updated_parsed=None),
]


def test_fetch_feed_maps_entries():
    with patch("feedparser.parse", return_value=FEED_XML_PARSED):
        arts = fetch_feed("https://ex.com/rss", "example")
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
        arts = fetch_feed("https://ex.com/rss", "example")
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
    parsed = MagicMock(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        arts = fetch_feed("https://ex.com/atom", "example")
    assert arts[0].published == datetime(2026, 7, 21, 9, 30, 45,
                                         tzinfo=timezone.utc)


def test_fetch_feed_skips_entry_missing_link():
    # SimpleNamespace は MagicMock と違い未設定属性へのアクセスで
    # AttributeError を送出する (feedparser の FeedParserDict と同じ挙動)。
    good = SimpleNamespace(link="https://ex.com/a1", title="T1",
                           summary="s1", published_parsed=None)
    bad = SimpleNamespace(title="no link", summary="s2",
                          published_parsed=None)
    parsed = MagicMock(entries=[good, bad])
    with patch("feedparser.parse", return_value=parsed):
        arts = fetch_feed("https://ex.com/rss", "example")
    assert len(arts) == 1
    assert arts[0].url == "https://ex.com/a1"


def test_fetch_feed_missing_title_defaults_to_empty_string():
    entry = SimpleNamespace(link="https://ex.com/a1", summary="s1",
                            published_parsed=None)
    parsed = MagicMock(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        arts = fetch_feed("https://ex.com/rss", "example")
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
    parsed = MagicMock(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            arts = fetch_feed("https://ex.com/rss", "example")
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
    parsed = MagicMock(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            fetch_feed("https://ex.com/rss", "example")
    assert caplog.records == []


def test_fetch_feed_no_warning_for_known_abbreviation(caplog):
    entry = MagicMock(
        link="https://ex.com/a", title="t", summary="",
        published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0),
        published="Wed, 22 Jul 2026 10:00:00 GMT")
    parsed = MagicMock(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            fetch_feed("https://ex.com/rss", "example")
    assert caplog.records == []


def test_fetch_feed_no_warning_when_raw_date_missing(caplog):
    # SimpleNamespace で「raw な日付文字列が本当に無い」を再現する
    # (MagicMock は未設定属性を自動生成し str 以外の値を返すため、
    # 実装の isinstance ガードで別経路を通ってしまい、この経路を確認できない)。
    entry = SimpleNamespace(
        link="https://ex.com/a", title="t", summary="",
        published_parsed=(2026, 7, 22, 10, 0, 0, 2, 203, 0))
    parsed = MagicMock(entries=[entry])
    with patch("feedparser.parse", return_value=parsed):
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            arts = fetch_feed("https://ex.com/rss", "example")
    assert caplog.records == []
    assert arts[0].published is not None


def test_fetch_web_extracts_body():
    resp = MagicMock(text="<html>...</html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as get, \
         patch("trafilatura.extract", return_value="本文テキスト") as ext, \
         patch("trafilatura.extract_metadata") as meta:
        meta.return_value = MagicMock(title="記事タイトル")
        arts = fetch_web("https://ex.com/page", "example")
    assert len(arts) == 1
    assert arts[0].body == "本文テキスト"
    assert arts[0].title == "記事タイトル"
    assert arts[0].url == "https://ex.com/page"
    assert arts[0].source_name == "example"
    assert arts[0].published is None
    get.assert_called_once_with("https://ex.com/page", timeout=30,
                                follow_redirects=True)
    # コメント欄が本文に混入しないこと (ブリーフの逐語コードからの意図的変更)
    assert ext.call_args.kwargs.get("include_comments") is False


def test_fetch_web_title_falls_back_to_url_when_no_metadata():
    resp = MagicMock(text="<html>...</html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp), \
         patch("trafilatura.extract", return_value="本文テキスト"), \
         patch("trafilatura.extract_metadata", return_value=None):
        arts = fetch_web("https://ex.com/page", "example")
    assert arts[0].title == "https://ex.com/page"


def test_fetch_web_extract_failure_returns_empty():
    resp = MagicMock(text="<html></html>")
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp), \
         patch("trafilatura.extract", return_value=None):
        assert fetch_web("https://ex.com/page", "example") == []


def test_fetch_web_propagates_http_error():
    # 404 等はここで握りつぶさず送出する (呼び出し側/将来の集約層が
    # ソース単位の fail-open を担当する。本関数の責務ではない)。
    resp = MagicMock(text="")
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404))
    with patch("httpx.get", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            fetch_web("https://ex.com/page", "example")

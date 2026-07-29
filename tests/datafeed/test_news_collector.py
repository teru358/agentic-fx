import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import FixedClock
from agentic_fx.datafeed.fetchers import Article
from agentic_fx.datafeed.news_collector import (
    DEFAULT_SOURCES, NewsCollector, seed_default_sources,
)
from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

ART = Article(url="https://ex.com/a1", title="t", body="b",
              published=None, source_name="s1")


def _env(tmp_path):
    from tests.store.test_rag import FakeEmbedding
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=FakeEmbedding())
    col = NewsCollector(conn, rag, ActivityLog(tmp_path / "a.log"),
                        FixedClock(NOW))
    return conn, rag, col


def test_seed_defaults_idempotent(tmp_path):
    conn, _, _ = _env(tmp_path)
    n1 = seed_default_sources(conn, NOW)
    assert n1 == len(DEFAULT_SOURCES) >= 2
    assert seed_default_sources(conn, NOW) == 0  # 冪等
    rows = news_sources.list_all(conn)
    assert all(s["enabled"] for s in rows)
    # ブリーフが太字で指定する要件 (「enabled=True, added_by="user" で
    # 未登録分のみ INSERT」)。added_by の値には元々どのテストも掛かって
    # いなかった (レビューの変異テストで agent に変えても全緑と判明) ので
    # ここでピンを打つ。
    assert all(s["added_by"] == "user" for s in rows)
    # 再レビューで判明した同型の穴: fetcher / url にもピンが無く、
    # `url=s["name"]` (DB に URL でなく名前が入る) や fetcher の固定値化に
    # 変えても全テストが緑だった。DEFAULT_SOURCES の各フィールドが
    # そのまま DB に載ることを name をキーに突き合わせる。
    by_name = {s["name"]: s for s in rows}
    for src in DEFAULT_SOURCES:
        assert by_name[src["name"]]["url"] == src["url"]
        assert by_name[src["name"]]["fetcher"] == src["fetcher"]


def test_seed_defaults_dedupes_within_default_sources_list(tmp_path, monkeypatch):
    """DEFAULT_SOURCES 自体の中で name が重複していても IntegrityError に
    ならないこと。

    修正ラウンド 1 で発見: existing_names/urls をループ開始前に 1 回だけ
    スナップショットすると、リスト内部での重複は防げない (2 件目を
    挿入しようとする時点で、同じループで挿入したばかりの 1 件目をまだ
    「既存」として知らないため)。ループ内で集合を更新して初めて閉じる。
    """
    conn, _, _ = _env(tmp_path)
    dup_sources = [
        {"name": "dup-src", "fetcher": "feed", "url": "https://a.example/rss"},
        {"name": "dup-src", "fetcher": "feed", "url": "https://b.example/rss"},
    ]
    monkeypatch.setattr("agentic_fx.datafeed.news_collector.DEFAULT_SOURCES",
                        dup_sources)
    n = seed_default_sources(conn, NOW)  # 例外を送出しないこと
    assert n == 1
    assert [s["name"] for s in news_sources.list_all(conn)] == ["dup-src"]


def test_seed_defaults_dedupes_url_collision_within_list(tmp_path, monkeypatch):
    """上と対になるケース: DEFAULT_SOURCES 内で **url** だけが重複する。

    name 側だけを補ったつもりでも url 側の集合更新を落とすと、db.py:92 の
    url UNIQUE で IntegrityError になり init が落ちる。name 衝突のケース
    だけでは `existing_urls.add(...)` にピンが掛からない (変異テストで
    実測: url 側の更新だけ削除しても name 衝突のテストは緑のままだった)。
    """
    conn, _, _ = _env(tmp_path)
    dup_sources = [
        {"name": "src-a", "fetcher": "feed", "url": "https://same.example/rss"},
        {"name": "src-b", "fetcher": "feed", "url": "https://same.example/rss"},
    ]
    monkeypatch.setattr("agentic_fx.datafeed.news_collector.DEFAULT_SOURCES",
                        dup_sources)
    n = seed_default_sources(conn, NOW)  # 例外を送出しないこと
    assert n == 1
    assert [s["name"] for s in news_sources.list_all(conn)] == ["src-a"]


def test_seed_defaults_skips_on_name_or_url_collision(tmp_path):
    """name/url どちらか片方だけの衝突でも IntegrityError を起こさないこと。

    schema は name と url の両方に UNIQUE 制約を持つ。url だけを見て
    重複判定すると、name が同じで url を修正した既存行 (例: 到達性確認の
    結果 URL を差し替えた場合) に対して再度 INSERT を試み、name の
    UNIQUE 制約で落ちる。
    """
    conn, _, _ = _env(tmp_path)
    first_name = DEFAULT_SOURCES[0]["name"]
    news_sources.add(conn, name=first_name, fetcher="feed",
                     url="https://different-url.example/rss",
                     added_by="user", now=NOW, enabled=True)
    n = seed_default_sources(conn, NOW)  # 例外を送出しないこと
    assert n == len(DEFAULT_SOURCES) - 1


def test_collect_fetches_enabled_sources(tmp_path):
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="s1", fetcher="feed", url="https://ex.com/rss",
                     added_by="user", now=NOW, enabled=True)
    news_sources.add(conn, name="s2", fetcher="feed", url="https://ex.com/off",
                     added_by="user", now=NOW, enabled=False)
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               return_value=[ART]) as f:
        total = col.collect()
    f.assert_called_once_with("https://ex.com/rss", "s1")  # 引数の順序・enabled のみ
    assert total == 1
    assert rag.count_news() == 1


def test_collect_sums_across_multiple_sources(tmp_path):
    """2 つの enabled ソースがそれぞれ記事を返したら合計されること。

    1 ソースしか成功しないテストだけだと `total += ...` を `total = ...`
    に変異させても検知できない (両方とも 1 になる)。
    """
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="s1", fetcher="feed", url="https://ex.com/rss1",
                     added_by="user", now=NOW, enabled=True)
    news_sources.add(conn, name="s2", fetcher="web", url="https://ex.com/page2",
                     added_by="user", now=NOW, enabled=True)
    art2 = Article(url="https://ex.com/a2", title="t2", body="b2",
                   published=None, source_name="s2")
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               return_value=[ART]), \
         patch("agentic_fx.datafeed.news_collector.fetch_web",
               return_value=[art2]):
        total = col.collect()
    assert total == 2
    assert rag.count_news() == 2


def test_collect_skips_unknown_fetcher(tmp_path):
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="weird", fetcher="bogus", url="https://ex.com/x",
                     added_by="agent", now=NOW, enabled=True)
    with patch("agentic_fx.datafeed.news_collector.fetch_feed") as ff, \
         patch("agentic_fx.datafeed.news_collector.fetch_web") as fw:
        total = col.collect()
    ff.assert_not_called()
    fw.assert_not_called()
    assert total == 0
    assert rag.count_news() == 0


def test_collect_survives_source_failure(tmp_path):
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="bad", fetcher="feed", url="https://bad",
                     added_by="user", now=NOW, enabled=True)
    news_sources.add(conn, name="good", fetcher="web", url="https://good",
                     added_by="user", now=NOW, enabled=True)
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               side_effect=OSError("down")), \
         patch("agentic_fx.datafeed.news_collector.fetch_web",
               return_value=[ART]):
        total = col.collect()
    assert total == 1  # bad はスキップ


def test_collect_calls_cleanup_and_writes_activity(tmp_path):
    """cleanup_news(48h) が collect の度に呼ばれ、activity に記録が残ること。

    brief の逐語テストには無い観点だが、契約 (「ニュース RAG は 48h で掃除」
    「activity NEWS 記録」) を検証しないと、これらの行を丸ごと消しても
    緑のままになってしまう (直前タスクの教訓: 動くことではなく壊れたら
    落ちることをテストする)。
    """
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="s1", fetcher="feed", url="https://ex.com/rss",
                     added_by="user", now=NOW, enabled=True)
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               return_value=[ART]), \
         patch.object(rag, "cleanup_news", wraps=rag.cleanup_news) as cleanup:
        col.collect()
    cleanup.assert_called_once_with(NOW, hours=48)

    lines = col.activity.tail(10)
    assert len(lines) == 1
    assert "\tNEWS\t" in lines[0]


def test_collect_error_message_does_not_leak_url(tmp_path):
    """ソース失敗のログに生の例外文字列 (URL を含みうる) をそのまま載せない。

    news_sources.url は将来 added_by="agent" のソースが query string に
    API key を持つ URL を登録するケースもありうる (Twelve Data と同型の
    リスク)。price_provider._safe_error_text と同じ配慮を collector 側でも
    要求する (ブリーフになかった観点、タスク指示に基づき追加)。
    """
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="bad", fetcher="web",
                     url="https://bad.example/feed?apikey=SECRET123",
                     added_by="user", now=NOW, enabled=True)
    with patch("agentic_fx.datafeed.news_collector.fetch_web",
               side_effect=RuntimeError(
                   "boom https://bad.example/feed?apikey=SECRET123")), \
         patch("agentic_fx.datafeed.news_collector._log") as mock_log:
        col.collect()
    logged = " ".join(str(c) for c in mock_log.warning.call_args_list)
    assert "SECRET123" not in logged


def test_collect_records_dead_feed_as_failure_and_continues(tmp_path, caplog):
    """修正ラウンド 1: 死んだフィード (bozo=True, entries=[]) が collect を
    通じて技術ログに残り、他ソースの取り込みは続くこと。

    ここでは `fetch_feed` 自体はモックせず (news_collector.fetch_feed を
    patch しない)、feedparser.parse だけを差し替えて実物の fetch_feed を
    通す — fetchers.py が FeedFetchError を送出し、それを collect の
    per-source except が拾うところまでを end-to-end で確認する。

    **どのモジュールが記録したかまで見る** (再レビュー指摘): fetchers と
    news_collector は同じ logger 名 (`agentic_fx.news`) を使うため、
    メッセージ本文だけを見ると「fetchers が自分で warning を出しただけ」
    でも通ってしまい、`collect` の except 経路を検証できていなかった
    (実測: raise を同文面の warning に置換してもこのテストは緑のままだった)。
    `record.module` で発生源を区別する。
    """
    conn, rag, col = _env(tmp_path)
    news_sources.add(conn, name="dead", fetcher="feed",
                     url="https://dead.example/rss",
                     added_by="user", now=NOW, enabled=True)
    news_sources.add(conn, name="good", fetcher="web", url="https://good",
                     added_by="user", now=NOW, enabled=True)
    # 実 feedparser がネットワーク失敗時に bozo_exception へ入れるのは
    # urllib.error.URLError (再レビューが実接続で確認)。fixture も実物に合わせる。
    dead_parsed = MagicMock(bozo=True, entries=[],
                            bozo_exception=URLError("Connection refused"))
    with patch("feedparser.parse", return_value=dead_parsed), \
         patch("agentic_fx.datafeed.news_collector.fetch_web",
               return_value=[ART]), \
         caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
        total = col.collect()
    assert total == 1  # good だけ取り込まれる。dead は落ちない
    from_collector = [r.getMessage() for r in caplog.records
                      if r.module == "news_collector"]
    assert any("dead" in m and "failed" in m for m in from_collector)
    messages = [r.getMessage() for r in caplog.records]
    assert "dead.example" not in " ".join(messages)  # URL は出さない

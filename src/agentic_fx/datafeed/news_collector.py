"""NewsCollector — news_sources を回して RAG へ。scheduler の on_news_cycle 実装。

ニュースは価格と違い、取れなくても取引を止める種類のデータではない
(グローバル制約)。1 ソースの失敗で collector 全体を落とさず、取れた
ものは取り込み、失敗は技術ログに warning として残すだけにする
(fail-open)。一方で時刻の正しさは妥協しない — fetchers.py / rag.py が
tz-aware UTC を強制する経路はそのまま素通しし、ここで naive を UTC と
みなす処理は一切行わない。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

import httpx

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.datafeed.default_sources import DEFAULT_SOURCES
from agentic_fx.datafeed.fetchers import fetch_feed, fetch_web
from agentic_fx.store import news_sources
from agentic_fx.store.rag import Rag

_log = logging.getLogger("agentic_fx.news")

__all__ = ["DEFAULT_SOURCES", "NewsCollector", "seed_default_sources"]

# price_provider.py の _safe_error_text と同じパターン (多層防御)。
# collector は複数ソースの失敗を集約してログに残すため、ソース URL に
# 秘密が含まれるケース (added_by="agent" で追加された将来のソース等) を
# 想定して同じ配慮を独立に持つ。private ヘルパーをモジュール間で import
# するのではなく複製する方針は rag.py の _require_utc (sources.py の
# _to_utc と同方針だが複製) に倣う。
_SECRET_RE = re.compile(r"((?:api[-_]?key|apikey|token|secret)=)[^&\s'\"]+",
                        re.IGNORECASE)


def _safe_error_text(e: BaseException) -> str:
    """例外を「技術ログに出してよい」文字列にする (URL を出さない)。"""
    if isinstance(e, httpx.HTTPStatusError):
        text = f"{type(e).__name__}: HTTP {e.response.status_code}"
    elif isinstance(e, httpx.HTTPError):
        text = type(e).__name__
    else:
        text = f"{type(e).__name__}: {e}"
    return _SECRET_RE.sub(r"\1***", text)


def seed_default_sources(conn: sqlite3.Connection, now: datetime) -> int:
    """DEFAULT_SOURCES のうち未登録 (name/url いずれも未登録) の分だけ INSERT する。

    enabled=True, added_by="user" で登録する (素の clone でも即取り込み
    対象になる)。冪等 — 既に name か url のどちらかが登録済みのものは
    触らない (name/url の両方が UNIQUE 制約なので、片方だけ見て事前
    チェックすると、name はそのままで url だけ差し替えた既存行 (例:
    到達性確認の結果 nhk-keizai の url だけ修正した場合) に対して
    再度 INSERT を試み、name の UNIQUE 制約で sqlite3.IntegrityError に
    なり init 自体が落ちる)。
    """
    existing_names = {s["name"] for s in news_sources.list_all(conn)}
    existing_urls = {s["url"] for s in news_sources.list_all(conn)}
    added = 0
    for s in DEFAULT_SOURCES:
        if s["name"] not in existing_names and s["url"] not in existing_urls:
            news_sources.add(conn, name=s["name"], fetcher=s["fetcher"],
                             url=s["url"], added_by="user", now=now,
                             enabled=True)
            added += 1
    return added


class NewsCollector:
    """enabled な news_sources を全件回し、fetcher で取得して RAG へ upsert する。"""

    def __init__(self, conn: sqlite3.Connection, rag: Rag,
                 activity: ActivityLog, clock: Clock) -> None:
        self.conn = conn
        self.rag = rag
        self.activity = activity
        self.clock = clock

    def collect(self) -> int:
        """1 サイクル分の収集を実行する。取得記事総数を返す。

        個別ソースの失敗 (フェッチ層の例外) はスキップして次のソースへ
        進む — 1 ソースの障害で全体を止めない (グローバル制約)。
        """
        now = self.clock.now()
        total = 0
        for src in news_sources.list_enabled(self.conn):
            try:
                if src["fetcher"] == "feed":
                    fetch = fetch_feed
                elif src["fetcher"] == "web":
                    fetch = fetch_web
                else:
                    # news_sources.add / スキーマは fetcher の値を検証しない
                    # (CHECK 制約なし)。added_by="agent" の将来のソースが
                    # 未知の fetcher 名で登録される可能性があるため、feed/web
                    # 以外を fetch_web に無言でフォールバックさせない
                    # (未知形式に httpx+trafilatura を適用すると壊れた
                    # 抽出結果が記事として RAG に混入しうる)。
                    _log.warning("news source %s: unknown fetcher %r, skipped",
                                src["name"], src["fetcher"])
                    continue
                articles = fetch(src["url"], src["name"])
                total += self.rag.add_news(
                    [{"url": a.url, "title": a.title, "body": a.body,
                      "source_name": a.source_name, "published": a.published}
                     for a in articles], now)
            except Exception as e:  # noqa: BLE001 — 1 ソース障害で止めない
                _log.warning("news source %s failed: %s", src["name"],
                            _safe_error_text(e))
        removed = self.rag.cleanup_news(now, hours=48)
        self.activity.write(Category.NEWS, "collected",
                            f"{total} articles ({removed} cleaned)")
        return total

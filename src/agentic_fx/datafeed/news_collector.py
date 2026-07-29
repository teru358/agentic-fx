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
import sqlite3
from datetime import datetime

from agentic_fx._safe_error import safe_error_text as _safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.datafeed.default_sources import DEFAULT_SOURCES
from agentic_fx.datafeed.fetchers import fetch_feed, fetch_web
from agentic_fx.store import news_sources
from agentic_fx.store.rag import Rag

_log = logging.getLogger("agentic_fx.news")

__all__ = ["DEFAULT_SOURCES", "NewsCollector", "seed_default_sources"]

# 秘密抑止は datafeed/_safe_error.py の safe_error_text に一本化した
# (Task 8)。collector は複数ソースの失敗を集約してログに残すため、ソース
# URL に秘密が含まれるケース (added_by="agent" で追加された将来のソース等)
# を想定して同じ配慮が要る。以前はここに同じ関数を複製していたが
# (「private ヘルパーはモジュール間で import せず複製する」方針)、econ
# カレンダーが 3 つ目の複製先になる時点でその方針は割に合わない —
# 抑止パターンを 1 箇所で更新できないと片側だけ古いままになる。


def seed_default_sources(conn: sqlite3.Connection, now: datetime) -> int:
    """DEFAULT_SOURCES のうち未登録 (name/url いずれも未登録) の分だけ INSERT する。

    enabled=True, added_by="user" で登録する (素の clone でも即取り込み
    対象になる)。冪等 — 既に name か url のどちらかが登録済みのものは
    触らない (name/url の両方が UNIQUE 制約なので、片方だけ見て事前
    チェックすると、name はそのままで url だけ差し替えた既存行 (例:
    到達性確認の結果 nhk-keizai の url だけ修正した場合) に対して
    再度 INSERT を試み、name の UNIQUE 制約で sqlite3.IntegrityError に
    なり init 自体が落ちる)。

    **修正ラウンド 1 (レビュー指摘)**: `existing_names`/`existing_urls` は
    ループの中で都度更新する。ループ前の 1 回だけのスナップショットだと
    `DEFAULT_SOURCES` **自体の中に** name/url の衝突があった場合に検知
    できない (2 件目を挿入しようとする時点で、まだ「同じループで挿入した
    1 件目」を知らないため)。あわせて `news_sources.list_all(conn)` は
    1 回だけ呼ぶ (以前は existing_names/urls それぞれで 1 回ずつ、計 2 回
    呼んでいた)。
    """
    existing = news_sources.list_all(conn)
    existing_names = {s["name"] for s in existing}
    existing_urls = {s["url"] for s in existing}
    added = 0
    for s in DEFAULT_SOURCES:
        if s["name"] in existing_names or s["url"] in existing_urls:
            continue
        news_sources.add(conn, name=s["name"], fetcher=s["fetcher"],
                         url=s["url"], added_by="user", now=now,
                         enabled=True)
        existing_names.add(s["name"])
        existing_urls.add(s["url"])
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
                    self._record_failure(
                        src["name"], f"unknown fetcher {src['fetcher']!r}")
                    continue
                articles = fetch(src["url"], src["name"])
                total += self.rag.add_news(
                    [{"url": a.url, "title": a.title, "body": a.body,
                      "source_name": a.source_name, "published": a.published}
                     for a in articles], now)
            except Exception as e:  # noqa: BLE001 — 1 ソース障害で止めない
                self._record_failure(src["name"], _safe_error_text(e))
        removed = self.rag.cleanup_news(now, hours=48)
        self.activity.write(Category.NEWS, "collected",
                            f"{total} articles ({removed} cleaned)")
        return total

    def _record_failure(self, source_name: str, detail: str) -> None:
        """ソース単位の失敗を技術ログ **と** activity の両方に残す。

        codex C-M4: 以前は技術ログ warning のみで、activity には成功
        (`collected`) しか載らなかった。恒常的に落ちているソース (URL 失効・
        未知 fetcher で登録されたエージェント追加ソース等) が人の目に触れる
        経路が無く、`collected 0 articles` が「今日はニュースが無い」と
        読めてしまう。EconCalendar._record_failure と同じ形に揃える。

        **fail-open は維持する** — この関数は記録するだけで、呼び出し側の
        制御フロー (スキップして次のソースへ) は変えない。detail は
        `safe_error_text` を通した文字列を渡すこと (ソース URL に秘密が
        含まれうる)。
        """
        _log.warning("news source %s failed: %s", source_name, detail)
        self.activity.write(Category.NEWS, "news_source_failed",
                            f"{source_name}: {detail}")

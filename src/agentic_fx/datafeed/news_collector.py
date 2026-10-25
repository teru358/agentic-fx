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
from datetime import datetime, timezone

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.datafeed.default_sources import DEFAULT_SOURCES
from agentic_fx.datafeed.fetchers import fetch_feed, fetch_web
from agentic_fx.datafeed.news_probe import classify_failure
from agentic_fx.store import fetch_attempts, news_health, news_sources
from agentic_fx.store.rag import Rag

_log = logging.getLogger("agentic_fx.news")

UNKNOWN_FETCHER_REASON = "未知の取得方式"

__all__ = ["DEFAULT_SOURCES", "NewsCollector", "seed_default_sources"]

# 秘密抑止は datafeed/_safe_error.py の safe_error_text に一本化した
# (Task 8)。collector は複数ソースの失敗を集約してログに残すため、ソース
# URL に秘密が含まれるケース (added_by="agent" で追加された将来のソース等)
# を想定して同じ配慮が要る。以前はここに同じ関数を複製していたが
# (「private ヘルパーはモジュール間で import せず複製する」方針)、econ
# カレンダーが 3 つ目の複製先になる時点でその方針は割に合わない —
# 抑止パターンを 1 箇所で更新できないと片側だけ古いままになる。


def seed_default_sources(conn: sqlite3.Connection, now: datetime, *, commit: bool = True) -> int:
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
                         enabled=True, commit=commit)
        existing_names.add(s["name"])
        existing_urls.add(s["url"])
        added += 1
    return added


class NewsCollector:
    """enabled な news_sources を全件回し、fetcher で取得して RAG へ upsert する。

    源ごとの取得結果は健康記録 (store/news_health) に残り、連続失敗や先方の
    拒否が続く源は段階的に間引かれる。健康記録を読めない周期は取得を見送る。
    """

    def __init__(self, conn: sqlite3.Connection, rag: Rag,
                 activity: ActivityLog, clock: Clock, *, timeout_sec: float) -> None:
        self.conn = conn
        self.rag = rag
        self.activity = activity
        self.clock = clock
        self.timeout_sec = timeout_sec
        self._skip_reported: dict[str, datetime] = {}
        # 書込に失敗した源の (新しい記録, 取得前に読んだ DB の原文)。DB の原文が
        # 組の原文と文字列として一致する間だけ、現在の記録として使う。
        self._health_overlay: dict[str, tuple[news_health.Health, str | None]] = {}
        self._gate_errors: set[str] = set()

    def collect(self) -> int:
        """1 サイクル分の収集を実行する。取得記事総数を返す。

        個別ソースの失敗 (フェッチ層の例外) はスキップして次のソースへ
        進む — 1 ソースの障害で全体を止めない (グローバル制約)。
        """
        now = self.clock.now()
        total = 0
        for src in news_sources.list_enabled(self.conn):
            name = src["name"]
            key = fetch_attempts.news_source_key(name)
            gate = self._gate(name, key, now)
            if gate is None:
                continue
            raw, current, wait = gate
            if wait is not None:
                self._report_skip(name, current, wait)
                continue
            self._skip_reported.pop(name, None)
            if src["fetcher"] == "feed":
                fetch = fetch_feed
            elif src["fetcher"] == "web":
                fetch = fetch_web
            else:
                # news_sources.add / スキーマは fetcher の値を検証しない
                # (CHECK 制約なし)。feed/web 以外を fetch_web に無言で
                # フォールバックさせない (壊れた抽出結果が記事として RAG に
                # 混入しうる)。要求は出さないので健康記録は更新しない。
                self._record_failure(name, UNKNOWN_FETCHER_REASON)
                continue
            self._remember_attempt(key, now)
            try:
                articles = fetch(src["url"], src["name"], timeout_sec=self.timeout_sec)
            except Exception as e:  # noqa: BLE001 — 1 ソース障害で止めない
                failure = classify_failure(e, now)
                self._remember_retry_after(key, failure, now)
                self._update_health(name, raw, current, failure.kind, now,
                                    reason=failure.reason)
                self._record_failure(name, failure.reason)
                continue
            try:
                total += self.rag.add_news(
                    [{"url": a.url, "title": a.title, "body": a.body,
                      "source_name": a.source_name, "published": a.published,
                      **({"rag_id": a.rag_id} if a.rag_id else {})}
                     for a in articles], now)
            except Exception as e:  # noqa: BLE001 — 保存側の障害は源の失敗に数えない
                _log.warning("news source %s store failed (%s)", name, type(e).__name__)
                self.activity.write(Category.NEWS, "news_store_failed", name)
                continue
            self._update_health(name, raw, current, "ok", now, count=len(articles))
        removed = self.rag.cleanup_news(now, hours=48)
        self.activity.write(Category.NEWS, "collected",
                            f"{total} articles ({removed} cleaned)")
        return total

    def _gate(self, name: str, key: str, now: datetime):
        """(取得前の原文, 現在の記録, 実効待機) を返す。読めなければ None (見送り)。"""
        try:
            observed = news_health.read(self.conn, name)
            observed = news_health.cap_backoff_for_collector(self.conn, name, observed, now)
            current = observed.record
            overlay = self._health_overlay.get(name)
            if overlay is not None:
                if overlay[1] == observed.raw:
                    current = overlay[0]
                else:
                    del self._health_overlay[name]
            attempt = news_health.load_attempt(self.conn, key)
            wait = news_health.effective_wait(current, attempt, now)
        except (sqlite3.Error, ValueError) as exc:
            if name not in self._gate_errors:
                self._gate_errors.add(name)
                _log.warning("news source %s: state unreadable, skipping (%s)",
                             name, type(exc).__name__)
            return None
        self._gate_errors.discard(name)
        return observed.raw, current, wait

    def _report_skip(self, name: str, current: news_health.Health | None,
                     wait: tuple[datetime, str]) -> None:
        until, cause = wait
        # 周期ごとに記録すると長い待ちで activity が埋まるので、期限ごとに 1 回
        if self._skip_reported.get(name) == until:
            return
        self._skip_reported[name] = until
        if cause == "failures":
            prefix = f"{name}: 連続 {current.consecutive_failures if current else 0} 回失敗のため "
        elif cause == "refusals":
            prefix = f"{name}: 先方の拒否が {current.retry_after_streak if current else 0} 回続いたため "
        else:
            prefix = f"{name}: 先方の指示により "
        self.activity.write(Category.NEWS, "news_source_skipped",
                            prefix + f"{until.astimezone(timezone.utc):%H:%M} UTC まで待つ")

    def _update_health(self, name: str, raw: str | None,
                       current: news_health.Health | None, outcome: str,
                       now: datetime, *, count: int | None = None,
                       reason: str | None = None) -> None:
        try:
            record = news_health.next_record(current, outcome, now, count=count, reason=reason)
        except ValueError as exc:
            _log.warning("news source %s health update failed (%s)", name, type(exc).__name__)
            return
        try:
            news_health.write_if_unchanged(self.conn, name, raw, record, now)
        except Exception as exc:  # noqa: BLE001 — 取得結果の扱いは止めない
            self._health_overlay[name] = (record, raw)
            _log.warning("news source %s health write failed (%s)", name, type(exc).__name__)
            return
        self._health_overlay.pop(name, None)

    def _remember_attempt(self, key: str, now: datetime) -> None:
        try:
            fetch_attempts.record_attempt(self.conn, key, now)
        except Exception as e:  # noqa: BLE001
            _log.warning("news attempt record failed (%s)", type(e).__name__)

    def _remember_retry_after(self, key: str, failure, now: datetime) -> None:
        if failure.retry_after is None:
            return
        try:
            fetch_attempts.record_retry_after(self.conn, key, now, failure.retry_after)
        except Exception as exc:  # noqa: BLE001 — 失敗の記録で失敗を増やさない
            _log.warning("news retry-after record failed (%s)", type(exc).__name__)

    def _record_failure(self, source_name: str, detail: str) -> None:
        """ソース単位の失敗を技術ログ **と** activity の両方に残す。

        codex C-M4: 以前は技術ログ warning のみで、activity には成功
        (`collected`) しか載らなかった。恒常的に落ちているソース (URL 失効・
        未知 fetcher で登録されたエージェント追加ソース等) が人の目に触れる
        経路が無く、`collected 0 articles` が「今日はニュースが無い」と
        読めてしまう。EconCalendar._record_failure と同じ形に揃える。

        **fail-open は維持する** — この関数は記録するだけで、呼び出し側の
        制御フロー (スキップして次のソースへ) は変えない。detail は固定語
        だけを渡すこと (例外の文面にはソース URL の秘密が含まれうる)。
        """
        _log.warning("news source %s failed: %s", source_name, detail)
        self.activity.write(Category.NEWS, "news_source_failed",
                            f"{source_name}: {detail}")

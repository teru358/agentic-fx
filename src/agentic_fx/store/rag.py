"""ChromaDB RAG — news (48h 掃除) / reflections の 2 コレクション (設計書 §12)。

reflections は `store/reflections.py` (SQLite) と意図的な二重保存 —
SQLite 側は order_id をキーにした確実な参照用、こちらは類似局面の
意味検索用 (設計書「ChromaDB」節: 「SQLite と二重保存」)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction


def _require_utc(dt: datetime, what: str) -> datetime:
    """tz-aware datetime を UTC に正規化する (naive は fail closed)。

    datafeed/sources.py の `_to_utc` と同じ方針 (プロジェクト制約:
    「naive を UTC とみなす」処理は禁止)。cleanup_news の判定は
    `added_at` 文字列の辞書順比較に依存しており、それが時系列順と
    一致するのは全レコードが同一オフセット (UTC) で isoformat() されて
    いる場合に限る。呼び出し側が tz-aware だが非 UTC (例: JST) の
    datetime を渡すと offset 表記が変わり比較が壊れるため、ここで
    明示的に UTC へ正規化してから isoformat() する。
    """
    if dt.tzinfo is None:
        raise ValueError(f"{what} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


class Rag:
    """news (48h 掃除) と reflections (トレード振り返り) の RAG ストア。"""

    def __init__(self, data_dir: Path, embedding_function=None) -> None:
        # embedding_function 未指定時は chromadb 既定 (all-MiniLM-L6-V2、
        # 初回呼び出し時にモデルを自動 DL) を使う。
        #
        # **ブリーフの逐語コードからの意図的な変更**: 逐語コードは
        # `get_or_create_collection` に embedding_function を渡さず既定に
        # 委ねていたが、chromadb 1.5.9 の `DefaultEmbeddingFunction.__call__`
        # はモデルのロード/DL を **初回呼び出し時まで遅延** する
        # (実機確認: オフライン環境でも `get_or_create_collection` 自体は
        # 常に成功し、失敗するのは最初の add/query 呼び出し時)。
        # 一方ブリーフの要求は「本番の初期化失敗 (モデル未配置+オフライン) は
        # Rag 生成時の例外として顕在化させる (黙って劣化しない)」— 逐語コードの
        # ままだと Rag() 自体は成功し、実際に落ちるのは最初の add_news/
        # search_news 呼び出し時になり要求を満たさない。ここで実体を確定させ
        # 明示的に 1 度呼び出す ("probe") ことで、要求どおり Rag 生成時点の
        # 例外にする。fake embedding (テスト) も同じコードパスを通るため
        # 本番/テストで分岐しない。
        ef = (embedding_function if embedding_function is not None
              else DefaultEmbeddingFunction())
        ef(["__afx_rag_init_probe__"])  # 失敗すればここで例外が飛ぶ

        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._news = self._client.get_or_create_collection(
            "news", embedding_function=ef)
        self._refl = self._client.get_or_create_collection(
            "reflections", embedding_function=ef)

    # ---- news -------------------------------------------------------------

    def add_news(self, articles: list[dict], now: datetime) -> int:
        """記事を upsert する (url を ID にして重複投入は上書き)。追加件数を返す。

        `published` は入力 dict のキーとして受け取るが保存しない:
        - search_news の出力契約 (url/title/body/source_name) に published は
          含まれない
        - `published` は None を取りうる (fetch_web は常に None。fetchers.py
          参照) が、chromadb の metadata は None 値を扱えないため、保存すると
          None の特別扱いが増えるだけで出力側に還元されない

        掃除 (cleanup_news) の基準は `published` ではなく取り込み時刻
        (`added_at`) に置く。理由: 組み込み fetcher の一方 (fetch_web) は
        published を常に None で返すため、published を基準にすると
        「published が無い記事は掃除できない (または None を特別扱いする
        独自ルールが要る)」問題が生じる。added_at はこのモジュールが常に
        tz-aware UTC で確定させるため、published の有無に関係なく一様に
        扱える。

        既知の限界: 同一 URL の記事が同じ内容のまま繰り返し add_news される
        (再クロールで同じ記事が返り続ける) と、upsert のたびに added_at が
        「今」に更新され、実質的に掃除されなくなる。組み込み 2 fetcher
        (feed/web) は通常「最新記事」だけを返すため実害は小さいと判断したが、
        将来 backfill 的な使い方をする fetcher を追加する場合は要再検討。
        """
        if not articles:
            return 0
        now_utc = _require_utc(now, "add_news now")
        self._news.upsert(
            ids=[a["url"] for a in articles],
            documents=[f"{a['title']}\n{a['body']}" for a in articles],
            metadatas=[{"url": a["url"], "title": a["title"],
                        "body": a["body"], "source_name": a["source_name"],
                        "added_at": now_utc.isoformat()}
                       for a in articles])
        return len(articles)

    def search_news(self, query: str, n: int = 5) -> list[dict]:
        """意味検索で news を引く。body は元記事の本文 (embedding 用に
        連結した "title\\nbody" テキストではない — メタデータに別途持つ)。
        """
        count = self.count_news()
        if count == 0:
            return []
        res = self._news.query(query_texts=[query], n_results=min(n, count))
        return [{"url": m["url"], "title": m["title"], "body": m["body"],
                 "source_name": m["source_name"]}
                for m in res["metadatas"][0]]

    def count_news(self) -> int:
        return self._news.count()

    def cleanup_news(self, now: datetime, hours: int = 48) -> int:
        """`added_at` が閾値より古い記事を削除する (設計書 §12)。削除件数を返す。"""
        now_utc = _require_utc(now, "cleanup_news now")
        cutoff = (now_utc - timedelta(hours=hours)).isoformat()
        got = self._news.get(include=["metadatas"])
        old = [i for i, m in zip(got["ids"], got["metadatas"])
               if m["added_at"] < cutoff]
        if old:
            self._news.delete(ids=old)
        return len(old)

    # ---- reflections --------------------------------------------------------

    def add_reflection(self, order_id: int, content: str, pair: str) -> None:
        self._refl.upsert(ids=[str(order_id)], documents=[content],
                          metadatas=[{"order_id": order_id, "pair": pair}])

    def search_reflections(self, query: str, n: int = 5) -> list[dict]:
        count = self._refl.count()
        if count == 0:
            return []
        res = self._refl.query(query_texts=[query], n_results=min(n, count))
        return [{"order_id": m["order_id"], "pair": m["pair"], "content": d}
                for d, m in zip(res["documents"][0], res["metadatas"][0])]

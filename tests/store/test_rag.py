import hashlib

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store.rag import Rag

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


class FakeEmbedding:
    """決定論的 fake embedding (ネットワーク・モデル DL 不要)。

    chromadb 1.5.9 は非 DefaultEmbeddingFunction のクエリ埋め込みに
    `embed_query` を要求する (`chromadb.api.types.EmbeddingFunction` を
    継承していれば `__call__` に委譲する既定実装が `__init_subclass__` で
    自動付与されるが、ここでは Protocol を継承しない素の duck-typing
    クラスのため明示的に定義する必要がある。ブリーフの逐語コードには
    無かったメソッドだが、無いと `search_news`/`search_reflections` が
    `AttributeError` になり実装できない — インストール済み chromadb
    バージョンとブリーフ記載コードの API 差分)。
    """

    def __call__(self, input):  # noqa: A002 — chromadb の EF 規約
        out = []
        for text in input:
            h = hashlib.sha256(text.encode()).digest()
            out.append([b / 255.0 for b in h[:16]])
        return out

    def embed_query(self, input):  # noqa: A002
        return self(input)

    def name(self):
        return "fake"


class BrokenEmbedding:
    """モデル未配置+オフライン相当 — 呼ばれると必ず失敗する。"""

    def __call__(self, input):  # noqa: A002
        raise RuntimeError("model not available (offline)")

    def name(self):
        return "broken"


def _rag(tmp_path):
    return Rag(tmp_path / "rag", embedding_function=FakeEmbedding())


ARTS = [
    {"url": "https://ex.com/a1", "title": "Dollar rallies on CPI",
     "body": "The US dollar strengthened after CPI data.",
     "source_name": "ex", "published": None},
    {"url": "https://ex.com/a2", "title": "BOJ keeps rates",
     "body": "Bank of Japan kept interest rates unchanged.",
     "source_name": "ex", "published": None},
]


def test_add_and_search(tmp_path):
    rag = _rag(tmp_path)
    assert rag.add_news(ARTS, NOW) == 2
    hits = rag.search_news("US dollar CPI", n=1)
    assert len(hits) == 1
    assert hits[0]["url"] in {"https://ex.com/a1", "https://ex.com/a2"}


def test_search_news_returns_original_body_not_embedding_document(tmp_path):
    """search_news の body は元記事の本文そのもの (embedding 用に連結した
    "title\\nbody" テキストではない) こと。連結文字列をそのまま返すと
    呼び出し側 (LLM プロンプト) にタイトルが二重に混入する。"""
    rag = _rag(tmp_path)
    rag.add_news(ARTS, NOW)
    hits = rag.search_news("US dollar CPI", n=2)
    by_url = {h["url"]: h for h in hits}
    assert by_url["https://ex.com/a1"]["body"] == (
        "The US dollar strengthened after CPI data.")


def test_upsert_dedup_by_url(tmp_path):
    rag = _rag(tmp_path)
    rag.add_news(ARTS, NOW)
    rag.add_news(ARTS, NOW)  # 再投入
    assert rag.count_news() == 2


def test_cleanup_removes_old(tmp_path):
    rag = _rag(tmp_path)
    rag.add_news([ARTS[0]], NOW - timedelta(hours=50))
    rag.add_news([ARTS[1]], NOW)
    removed = rag.cleanup_news(NOW, hours=48)
    assert removed == 1
    assert rag.count_news() == 1
    # 残っているのが「新しい方 (a2)」であること (どちらが消えたかまで確認する
    # — count だけの検証だと掃除条件を逆にしても件数が偶然一致してすり抜ける)
    remaining = rag.search_news("BOJ Dollar", n=2)
    assert {h["url"] for h in remaining} == {"https://ex.com/a2"}


def test_cleanup_keeps_article_with_published_none_if_recently_added(tmp_path):
    """published=None の記事でも掃除の基準は取り込み時刻 (added_at) — 直近に
    取り込んだものは published が無くても消えないこと。"""
    rag = _rag(tmp_path)
    rag.add_news([ARTS[0]], NOW)  # published=None, 取り込みは NOW (新しい)
    removed = rag.cleanup_news(NOW + timedelta(hours=1), hours=48)
    assert removed == 0
    assert rag.count_news() == 1


def test_add_news_rejects_naive_datetime(tmp_path):
    rag = _rag(tmp_path)
    with pytest.raises(ValueError):
        rag.add_news(ARTS, datetime(2026, 7, 22, 12, 0))  # tzinfo なし


def test_cleanup_news_rejects_naive_datetime(tmp_path):
    rag = _rag(tmp_path)
    rag.add_news(ARTS, NOW)
    with pytest.raises(ValueError):
        rag.cleanup_news(datetime(2026, 7, 22, 12, 0))  # tzinfo なし


def test_init_failure_surfaces_at_construction(tmp_path):
    """embedding function が使えない (モデル未配置+オフライン相当) 場合、
    Rag() の構築時点で例外になること — add/search まで黙って進んで
    そこで初めて失敗する、という劣化を許さない。

    `RuntimeError` + メッセージまで見る (`pytest.raises(Exception)` だけだと
    無関係な TypeError 等でも通ってしまい、確認したい「オフライン起因の
    初期化失敗」を検証したことにならない)。"""
    with pytest.raises(RuntimeError, match="model not available"):
        Rag(tmp_path / "rag", embedding_function=BrokenEmbedding())


def test_reopen_persisted_dir(tmp_path):
    """同じ data_dir に対して Rag() を作り直しても (プロセス再起動相当)、
    永続化済みの news/reflections コレクションを読み直せること。
    PersistentClient を使う本モジュールの本来の用途 (サービス起動のたびに
    Rag(data_dir) を new する) を実際に検証する — 1 プロセス内で 1 回しか
    Rag を作らないテストだけでは、コレクション再オープン時の embedding_function
    設定の食い違い (chromadb が config を永続化し再構築を試みる経路) を
    見落とす。"""
    rag = _rag(tmp_path)
    rag.add_news(ARTS, NOW)
    rag.add_reflection(1, "USDJPY long stopped out due to CPI spike",
                       "USDJPY")
    del rag

    reopened = _rag(tmp_path)
    assert reopened.count_news() == 2
    hits = reopened.search_news("US dollar CPI", n=1)
    assert hits and hits[0]["url"].startswith("https://ex.com/")
    refl_hits = reopened.search_reflections("stopped out CPI", n=1)
    assert refl_hits and refl_hits[0]["order_id"] == 1


def test_add_news_accepts_full_article_body(tmp_path):
    """fetch_web/trafilatura が返すような長文本文でも劣化なく往復すること
    (テストの一文だけの body に慣れて、本文が長い実データで壊れる
    「滑らかな fixture」の罠を避ける)。"""
    rag = _rag(tmp_path)
    big_body = "CPI data moved the market. " * 4000  # 実記事相当の長さ
    article = {"url": "https://ex.com/big", "title": "Long piece",
               "body": big_body, "source_name": "ex", "published": None}
    assert rag.add_news([article], NOW) == 1
    hits = rag.search_news("CPI data moved the market", n=1)
    assert hits[0]["body"] == big_body


def test_reflections_roundtrip(tmp_path):
    rag = _rag(tmp_path)
    rag.add_reflection(1, "USDJPY long был stopped out due to CPI spike",
                       "USDJPY")
    hits = rag.search_reflections("stopped out CPI", n=1)
    assert hits and hits[0]["order_id"] == 1

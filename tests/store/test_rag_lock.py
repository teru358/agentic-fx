"""Rag のスレッド安全化 (プラン8, 設計書 §4.4 codex I-8)。"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone

import pytest

from agentic_fx.store.rag import Rag, RagUnavailable


class _FakeEmbedding:
    """決定論的 fake embedding (ネットワーク・モデル DL 不要)。

    chromadb 1.5.9 は非 DefaultEmbeddingFunction のクエリ埋め込みに
    `embed_query` を要求し、かつ __call__ の引数名を `input` と期待する。
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


def test_search_news_raises_rag_unavailable_when_lock_held(tmp_path):
    """他スレッドが lock を保持し続けている間、search_news は
    lock_timeout_sec 経過で RagUnavailable を送出する (無期限ブロックしない)。"""
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=0.2)

    release = threading.Event()

    def hold_lock():
        with rag._lock:  # 内部実装への直接アクセス (テスト専用の白箱検証)
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    time.sleep(0.05)  # hold_lock が確実に lock を取ってから測る
    try:
        with pytest.raises(RagUnavailable):
            rag.search_news("query")
    finally:
        release.set()
        t.join(timeout=2.0)


def test_close_calls_chromadb_client_close(tmp_path, monkeypatch):
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding())
    calls: list[str] = []
    monkeypatch.setattr(rag._client, "close", lambda: calls.append("closed"))
    rag.close()
    assert calls == ["closed"]


def test_close_raises_rag_unavailable_when_lock_held(tmp_path):
    """lock を保持した状態で close() を呼ぶと RagUnavailable を送出する
    (mutation 2 のピン: close 自体が lock を取ることの確認)。"""
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=0.2)

    release = threading.Event()

    def hold_lock():
        with rag._lock:
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    time.sleep(0.05)
    try:
        with pytest.raises(RagUnavailable):
            rag.close()
    finally:
        release.set()
        t.join(timeout=2.0)


def test_normal_operations_still_serialize_correctly(tmp_path):
    """lock 導入後も既存の add_news/search_news/cleanup_news の挙動が
    不変であることの回帰確認 (既存 tests/store/test_rag.py が主だが、
    ここでも 1 本だけ通しで確認する)。"""
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding())
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    added = rag.add_news(
        [{"url": "https://x/1", "title": "t", "body": "b",
         "source_name": "s", "published": None}], now)
    assert added == 1
    assert rag.count_news() == 1
    # search_news が count_news を内部呼び出しすることの確認（再入デッドロック回避）
    hits = rag.search_news("t b", n=5)
    assert len(hits) >= 1


@pytest.mark.parametrize("method_name", [
    "add_news", "search_news", "count_news", "cleanup_news",
    "add_reflection", "search_reflections"
])
def test_all_public_methods_raise_rag_unavailable_when_lock_held(tmp_path, method_name):
    """全 6 の公開メソッドが lock を取得できないと RagUnavailable を送出する
    (防御の適用範囲全体に)。"""
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=0.2)

    release = threading.Event()

    def hold_lock():
        with rag._lock:
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    time.sleep(0.05)

    try:
        with pytest.raises(RagUnavailable):
            if method_name == "add_news":
                rag.add_news([], datetime.now(timezone.utc))
            elif method_name == "search_news":
                rag.search_news("q")
            elif method_name == "count_news":
                rag.count_news()
            elif method_name == "cleanup_news":
                rag.cleanup_news(datetime.now(timezone.utc))
            elif method_name == "add_reflection":
                rag.add_reflection(1, "c", "p")
            elif method_name == "search_reflections":
                rag.search_reflections("q")
    finally:
        release.set()
        t.join(timeout=2.0)


def test_rag_unavailable_propagates_through_scheduler_tick(tmp_path):
    """RagUnavailable が scheduler._run_data_hook を通過して資金保護
    (_process_exits) に到達することの確認 (波及の pin: mutation 3)。

    prolog: このテストは既存 test_scheduler.py の Env fixture を再利用し、
    news_fn に RagUnavailable を投げる関数を渡す。Scheduler.tick が正常に
    完了する（資金保護まで実行される）ことを確認し、その後 _run_data_hook
    の try/except を外す変異を注入したら red になることを確認する。
    """
    from agentic_fx.store.rag import RagUnavailable
    from tests.core.test_scheduler import Env, WED

    def rag_unavailable():
        raise RagUnavailable("RAG locked")

    env = Env(tmp_path, news_fn=rag_unavailable)
    # Scheduler.tick が正常に完了すること（例外を tick に貫通させない）
    env.sched.tick(WED)
    # news_cycle は呼ばれているはず
    assert env.news_calls == 1
    # **tick が最後まで走ったこと** — 「例外が出ない」だけでは不十分で、
    # 何らかの理由で tick が早期 return しても緑になってしまう。
    # `on_trade_mission` は決定論ブロック (mark-to-market → 資金保護
    # `_process_exits`) の**後段**にあるため、ここが呼ばれていれば資金保護
    # まで到達したことが言える (既存 tests/core/test_scheduler.py が同じ
    # イディオムを使っている — 「tick は最後まで走った」)。
    # 2026-08-08 指揮者が追加: 実装者の版は「例外が出ない」までしか
    # 検証しておらず、資金保護への到達を pin していなかった。
    assert env.trade_calls == 1


# ---------------------------------------------------------------------------
# 指揮者検証 (2026-08-08) で見つかった生存変異 2 件への回帰ピン。
# ---------------------------------------------------------------------------


def test_lock_is_released_when_locked_body_raises(tmp_path):
    """`_locked()` の `finally` の pin。

    lock 保護下の本体が例外を送出したときに lock を解放しないと、**以後
    その `Rag` インスタンスは永久に使えなくなる** (どの公開メソッドも
    `lock_timeout_sec` 経過後に `RagUnavailable` を出し続ける)。chromadb の
    一時的なエラー 1 回で news 収集と reflection 書込が恒久停止する、という
    可用性の欠陥になる。

    実測: `finally` を外す変異は**全 1527 テストを素通りして生存**していた。
    """
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=0.2)
    from datetime import datetime

    # lock 保護下で例外を起こす (naive datetime → _require_utc が ValueError)。
    with pytest.raises(ValueError):
        rag.add_news([{"url": "https://x/1", "title": "t", "body": "b",
                       "source_name": "s", "published": None}],
                     datetime(2026, 8, 4))          # tz-naive

    # lock が解放されていれば、後続の呼び出しは通常どおり成功する。
    assert rag.count_news() == 0
    assert rag.search_news("q") == []


def test_build_app_wires_lock_timeout_from_worker_settings(tmp_path):
    """Step 5 の配線の pin (`settings.worker.rpc_timeout_sec` → `Rag`)。

    実測: この配線を外す変異は**全 1527 テストを素通りして生存**していた
    (「単体は緑でも配線は誰も検証していない」— プラン 8 Task 7 と同型)。
    """
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.service import build_app
    from tests.test_service_app import (
        NOW, FakeEmbedding, FakeRunner, _init)

    _init(tmp_path)          # config/settings.yaml 等を用意する既存ヘルパー
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    try:
        assert (app.rag._lock_timeout_sec
                == app.settings.worker.rpc_timeout_sec)
        # 既定値そのものと取り違えないよう、値が実際に settings 由来である
        # ことを別経路でも確かめる。
        assert app.settings.worker.rpc_timeout_sec == 15.0
    finally:
        app.conn_core.close()
        app.conn_shell.close()

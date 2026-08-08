"""Rag のスレッド安全化 (プラン8, 設計書 §4.4 codex I-8)。"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from types import SimpleNamespace
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
    holding = threading.Event()

    def hold_lock():
        with rag._lock:  # 内部実装への直接アクセス (テスト専用の白箱検証)
            holding.set()
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert holding.wait(timeout=2.0), "holder が lock を取得できなかった"
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
    holding = threading.Event()

    def hold_lock():
        with rag._lock:
            holding.set()
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert holding.wait(timeout=2.0), "holder が lock を取得できなかった"
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
    holding = threading.Event()

    def hold_lock():
        with rag._lock:
            holding.set()
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert holding.wait(timeout=2.0), "holder が lock を取得できなかった"

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

    # レビュー 1 周目 (codex + sonnet 一致): 既定値 (15.0) と一致することを
    # 見るだけでは、**設定を読まず 15.0 を直書きする変異を検出できない**
    # (両レビュアーが独立に実測して生存を確認)。`Rag` の既定 10.0 とも
    # `WorkerSettings` の既定 15.0 とも異なる値を設定に書いてから構築する。
    settings_path = tmp_path / "config" / "settings.yaml"
    text = settings_path.read_text()
    assert "rpc_timeout_sec" in text, "worker.rpc_timeout_sec が example に無い"
    text = re.sub(r"rpc_timeout_sec:\s*[0-9.]+", "rpc_timeout_sec: 0.37", text)
    settings_path.write_text(text)

    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    try:
        assert app.settings.worker.rpc_timeout_sec == 0.37   # 設定が効いている
        assert app.rag._lock_timeout_sec == 0.37             # Rag へ届いている
    finally:
        app.conn_core.close()
        app.conn_shell.close()


def test_lock_timeout_waits_and_succeeds_when_released_in_time(tmp_path):
    """`lock_timeout_sec` が **実際の待ち時間**として機能することの pin。

    既存テストは「deadline より長く保持すれば `RagUnavailable`」しか見て
    おらず、**`acquire(timeout=0)` (try-lock 化) が全 13 本を素通りして
    生存**していた (レビュー 1 周目で codex/sonnet が独立に実測)。
    単一 lock で全操作を直列化する設計では**短い競合は正常系**なので、
    0 秒化は可用性を実質的に変えてしまう。

    **決定論的なハンドシェイクで組む** (指揮者の 1 回目の修正は
    「holder が 0.05 秒保持 → main が呼ぶ」という順序だったため、main が
    呼ぶ前に holder が解放してしまい変異が生存したまま緑になった):

      1. holder が lock を取得し `holding` を立てる
      2. main は `holding` を待ち、`about_to_call` を立ててから呼ぶ
         (ここで **main は acquire でブロックする**)
      3. holder は `about_to_call` を待ってから 0.3 秒保持し続けて解放
      4. main の acquire が成功する (deadline 5.0 秒に対し 0.3 秒待ち)

    **このテストは load-bearing ではない** (レビュー 2 周目 codex): main が
    `about_to_call.set()` してから実際に `acquire` へ入るまでの間に holder が
    解放し切ってしまう経路が残っており、CI 負荷下では `timeout=0` 変異が
    false-green になりうる (0.4 秒の遅延注入で実測された)。**契約を pin して
    いるのは `test_locked_passes_the_configured_timeout_to_acquire` (引数)、
    `test_locked_actually_waits_for_the_configured_duration` (待ち時間)、
    `test_two_real_operations_contend_on_the_lock_that_guards_the_body`
    (観測している lock が実際に body を守っていること) の 3 本**で、
    こちらは「解放されれば待って成功する」という挙動の説明として残す。
    """
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=5.0)

    holding = threading.Event()
    about_to_call = threading.Event()
    released = threading.Event()

    def hold_until_main_blocks():
        with rag._lock:
            holding.set()
            # main が「これから呼ぶ」と宣言するまで待ち、そこからさらに
            # 保持し続ける → main は確実に acquire でブロックする。
            about_to_call.wait(timeout=5.0)
            time.sleep(0.3)
        released.set()

    th = threading.Thread(target=hold_until_main_blocks, daemon=True)
    th.start()
    try:
        assert holding.wait(timeout=5.0), "holder が lock を取得できなかった"
        about_to_call.set()
        # deadline (5.0s) 内に解放されるので、待って取得できるはず。
        assert rag.count_news() == 0
        assert released.wait(timeout=5.0)
    finally:
        about_to_call.set()      # 例外時も holder を確実に前進させる
        th.join(timeout=5.0)


class _SpyLock:
    """テスト専用の `threading.Lock` ラッパ (レビュー 2 周目の反映)。

    `_locked()` が `acquire` に渡した `timeout` 引数を記録する。

    **なぜ必要か**: 2 周目で両レビュアーが別々の穴を実測した —
    - sonnet: `acquire(timeout=self._lock_timeout_sec)` を **`timeout=1.0`
      の直書き**に変えても 14 本すべてが素通りした。設定値が
      `_lock_timeout_sec` に**格納**されることと `build_app` が**配線**する
      ことは pin されていたが、**`_locked()` がその値を実際に消費している**
      ことは誰も検証していなかった (「値は存在するが効いていない」— 1 周目の
      配線テストと同じクラスの穴)
    - codex: 時間ベースのハンドシェイクは **main が acquire に入る前に holder
      が解放しうる**ため、`timeout=0` 変異が CI 負荷下で false-green になる
      (`about_to_call.set()` 直後に 0.4 秒の遅延を注入して実測)

    引数を直接観測すれば**スレッドも時間も使わずに**両方を殺せる。

    `__enter__`/`__exit__` は生 lock を直接使う — holder 側の
    `with rag._lock:` が `timeouts` を汚さないようにするため。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.timeouts: list[float] = []
        # `acquire` 呼び出しの**内側**で計った経過時間 (レビュー 3 周目 codex)。
        # 外側 (public method の呼び出しを跨いだ計測) では、`started` 取得後
        # acquire 進入前の deschedule が経過を**水増し**するため、下限
        # assert (`elapsed >= 0.4`) が `timeout=0` 変異を false-green に
        # してしまう (0.45 秒の遅延注入で実測された)。acquire の直前・直後で
        # 計れば水増し経路が構造的に無くなる。
        self.waits: list[float] = []

    def acquire(self, blocking: bool = True, timeout: float = -1):
        self.timeouts.append(timeout)
        started = time.monotonic()
        try:
            return self._lock.acquire(blocking, timeout)
        finally:
            self.waits.append(time.monotonic() - started)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self._lock.release()
        return False


@pytest.mark.parametrize("configured", [0.37, 1.25])
def test_locked_passes_the_configured_timeout_to_acquire(tmp_path, configured):
    """`_locked()` が **`_lock_timeout_sec` の値そのものを** `acquire` へ
    渡すことの pin (レビュー 2 周目 sonnet + codex)。

    スレッドを使わないので **race が原理的に起きない**。2 つの値で
    パラメータ化しているため、どんな定数を直書きしても必ず一方で落ちる。
    `timeout=0` 変異も同時に殺す (記録が 0 になり設定値と一致しない)。
    """
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=configured)
    spy = _SpyLock()
    rag._lock = spy

    assert rag.count_news() == 0          # lock は空いているので即成功する
    assert spy.timeouts == [configured]


def test_locked_actually_waits_for_the_configured_duration(tmp_path):
    """`timeout` が **実際の待ち時間**として使われることの pin。

    **計測は `_SpyLock.acquire` の内側で行う** (レビュー 3 周目 codex)。
    指揮者の前版は public method 呼び出しの外側で `time.monotonic()` を
    取っており、「`started` の後・acquire 進入の前」に main が deschedule
    されると経過が**水増し**されて `timeout=0` 変異が green になった
    (0.45 秒の遅延注入で実測)。「遅延は経過を伸ばす方向にしか働かない」と
    いう前版の docstring の主張は**下限 assert に対しては逆に危険**だった。
    acquire の直前・直後で計れば、水増しの経路そのものが無くなる。
    """
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=0.5)
    spy = _SpyLock()
    rag._lock = spy

    holding = threading.Event()
    release = threading.Event()

    def hold_forever():
        with spy:                      # 生 lock を保持 (記録を汚さない)
            holding.set()
            release.wait(10.0)

    th = threading.Thread(target=hold_forever, daemon=True)
    th.start()
    try:
        assert holding.wait(timeout=5.0), "holder が lock を取得できなかった"
        with pytest.raises(RagUnavailable):
            rag.count_news()
        assert spy.timeouts == [0.5]
        # acquire の内側で 0.5 秒待ってから諦めたはず。`timeout=0` なら ~0。
        assert spy.waits[0] >= 0.4, f"待っていない (waits={spy.waits})"
    finally:
        release.set()
        th.join(timeout=5.0)


def test_two_real_operations_contend_on_the_lock_that_guards_the_body(tmp_path):
    """**観測している acquire が、実際に critical section を守っている
    acquire であること**の pin (レビュー 3 周目 codex Important 1)。

    これまでの pin はすべて「`rag._lock` を直接保持する holder」と
    「1 本の public operation」の競合を見ていた。そのため codex が実測した
    **おとり変異** — `self._lock` を configured timeout で取って即座に解放し
    (spy はこれを正しく観測する)、実際の相互排他は別の `_body_lock` を
    `timeout=0` で取って行う — が **全 1533 テストを生存**した。
    「値は存在し、配線され、**観測もされる**が、実運用の競合には
    load-bearing でない」という第 3 形態である。

    そこで **2 本の実 operation 同士**を競合させる。1 本目は critical
    section の内側 (`_count_news_unlocked` が呼ぶ `self._news.count`) で
    ブロックし続けるため、lock がどれであれ確実に保持される。

    **順序は spy で決定論的に固定する**: 2 本目が `_locked()` に入った
    こと (= spy が 2 回目の acquire を記録したこと) を確認してから 1 本目を
    解放する。おとり変異ではこの時点で 2 本目は既に `_body_lock` の
    try-lock に失敗しているため `RagUnavailable` になり red。
    正しい実装では 2 本目は待機中で、解放後に成功する。
    """
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding(),
              lock_timeout_sec=5.0)
    spy = _SpyLock()
    rag._lock = spy

    inside = threading.Event()
    release = threading.Event()
    real_count = rag._news.count

    def blocking_count():
        inside.set()
        release.wait(10.0)
        return real_count()

    rag._news = SimpleNamespace(count=blocking_count)

    out: dict = {}

    def op_first():
        try:
            rag.count_news()
        except Exception as e:          # noqa: BLE001
            out["first_exc"] = e

    def op_second():
        try:
            out["second"] = rag.count_news()
        except Exception as e:          # noqa: BLE001
            out["second_exc"] = e

    t1 = threading.Thread(target=op_first, daemon=True)
    t2 = threading.Thread(target=op_second, daemon=True)
    t1.start()
    try:
        assert inside.wait(timeout=5.0), "1 本目が critical section に入らない"
        t2.start()
        # 2 本目が `_locked()` に入る (spy が 2 件目を記録する) まで待つ。
        deadline = time.monotonic() + 5.0
        while len(spy.timeouts) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(spy.timeouts) == 2, f"2 本目が _locked() に入らない: {spy.timeouts}"
    finally:
        release.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

    assert "first_exc" not in out, f"1 本目が失敗した: {out.get('first_exc')}"
    assert "second_exc" not in out, (
        "2 本目が待たずに失敗した — 観測している acquire が実際の "
        f"critical section を守っていない: {out.get('second_exc')}")
    assert out["second"] == 0

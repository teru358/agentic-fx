"""WorkerRunner (プラン8, 設計書 §3.1/§4.1/§4.3/§4.7)。

FakeChild: 実 subprocess を spawn せず、os.pipe() で親子間パイプを模倣し、
別スレッドで「子のふり」をして handshake/ready/event/result を書く。
実 subprocess spawn の最小 E2E は本ファイル末尾の
test_real_subprocess_completes_mission_end_to_end のみ (他は全て
FakeChild 経由)。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.landlock import is_available as landlock_available
from agentic_fx.core.mission_protocol import write_frame
from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from datetime import datetime, timezone

SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _root(tmp_path):
    import shutil
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example",
        root / "config" / "settings.yaml")
    init_db(connect(root / "data" / "agentic.db"))
    return root


class _FakeEmbedding:
    """決定論的 fake embedding (ネットワーク・モデル DL 不要)。

    **2026-08-08 指揮者がプランの欠陥を修正**: プラン Step 1 の `_rag` は
    `embedding_function=lambda t: [[0.0] * 4 for _ in t]` を渡していたが、
    chromadb 1.5.9 は embedding function に `.name` 属性を要求するため
    lambda では `AttributeError` になる (実測: 11 テストが red)。さらに
    非 `DefaultEmbeddingFunction` のクエリ埋め込みには `embed_query` が
    必要で、`__call__` の引数名は `input` と期待される。
    このプロジェクトは同じ罠を過去に 2 度踏んでおり (`tests/store/test_rag.py`
    と `tests/store/test_rag_lock.py` の docstring に記録がある)、そこで
    確立したクラス形式に揃える。
    """

    def __call__(self, input):  # noqa: A002 — chromadb の EF 規約
        return [[0.0] * 4 for _ in input]

    def embed_query(self, input):  # noqa: A002
        return self(input)

    def name(self):
        return "fake-worker-runner-embedding"


def _rag(tmp_path):
    return Rag(tmp_path / "rag", embedding_function=_FakeEmbedding())


def _mission():
    return Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                   max_turns=1, timeout_sec=5.0)


def _worker_settings(*, claude_backend: bool = False,
                     credentials_file: str | None = None,
                     codex_backend: bool = False,
                     codex_provider: str | None = None,
                     codex_auth_file: str | None = None):
    """`SETTINGS` を deep copy し improve backend / claude credentials_file /
    codex provider・auth_file を上書きするテスト専用ヘルパ (Minor 5:
    プラン旧稿の `_settings(...)` は非実在だったため新規に書き起こす)。"""
    runner_update: dict = {}
    if claude_backend:
        runner_update["improve"] = SETTINGS.runner.improve.model_copy(
            update={"backend": "claude"})
    if credentials_file is not None:
        runner_update["claude"] = SETTINGS.runner.claude.model_copy(
            update={"credentials_file": credentials_file})
    if codex_backend:
        runner_update["improve"] = SETTINGS.runner.improve.model_copy(
            update={"backend": "codex"})
    codex_update: dict = {}
    if codex_provider is not None:
        codex_update["provider"] = codex_provider
    if codex_auth_file is not None:
        codex_update["auth_file"] = codex_auth_file
    if codex_update:
        runner_update["codex"] = SETTINGS.runner.codex.model_copy(
            update=codex_update)
    if not runner_update:
        return SETTINGS
    return SETTINGS.model_copy(
        update={"runner": SETTINGS.runner.model_copy(update=runner_update)})


def test_mission_worker_env_excludes_credentials_for_improve(monkeypatch):
    """improve profile では資格情報も渡さない (裁定書 F-9 — 遮断維持)。

    IM-3/P8-03 対応で trade profile にのみ env allowlist していたが、
    プラン 10 (R10-①) で trade も env 経由をやめ handshake の
    `credentials` フィールドへ移した — 現在はどの profile も
    `_mission_worker_env` からは資格情報を受け取らない (旧
    `test_mission_worker_env_includes_data_provider_credentials_for_trade`
    は本変更で削除。trade 側の新しい契約は
    `test_trade_worker_no_longer_receives_data_provider_keys_via_env` /
    `test_trade_worker_receives_data_provider_keys_via_handshake` が持つ)。"""
    from agentic_fx.runners.worker_runner import _mission_worker_env

    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-secret")
    monkeypatch.setenv("MT5_BRIDGE_API_KEY", "mt5-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")  # 5-E M4 killer (検収 B2)

    env = _mission_worker_env("improve")

    assert "TWELVEDATA_API_KEY" not in env
    assert "MT5_BRIDGE_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_mission_worker_env_omits_unset_credentials(monkeypatch):
    """親環境にキーが無ければそもそも env に含めない (空文字を渡さない)。"""
    from agentic_fx.runners.worker_runner import _mission_worker_env

    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    monkeypatch.delenv("MT5_BRIDGE_API_KEY", raising=False)

    env = _mission_worker_env("trade")

    assert "TWELVEDATA_API_KEY" not in env
    assert "MT5_BRIDGE_API_KEY" not in env


class _FakeChildScript:
    """テストが `subprocess.Popen` の代わりに使う擬似子プロセス。実
    プロセスは起動しない — 親側 (WorkerRunner) が書く stdin をこのスレッドが
    読み、指定したフレーム列を stdout 側パイプへ書き込む。"""

    def __init__(self, frames_after_handshake, *, delay_before_result=0.0):
        self.frames = frames_after_handshake
        self.delay = delay_before_result
        self.pid = 999999  # WorkerRunner が expected_parent_pid の照合対象に
                            # しない値 (FakeChild は本物の os.getppid() 照合を
                            # 経由しない — インプロセステストのため)
        self.returncode = None
        self._killed = threading.Event()

    def poll(self):
        return None if not self._killed.is_set() else -9

    def wait(self, timeout=None):
        self._killed.wait(timeout)
        return -9 if self._killed.is_set() else None

    def kill_signal_received(self):
        self._killed.set()


def test_worker_runner_completes_mission_via_pipes(tmp_path, monkeypatch):
    """正常系: handshake→ready→event×2→result の往復を検証する。"""
    r, w = os.pipe()   # 子→親 (子が書く側 = w、親が読む側 = r)
    r2, w2 = os.pipe()  # 親→子 (親が書く側 = w2、子が読む側 = r2)

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "event", "seq": 2,
                                "message": {"role": "user", "content": "hi"}})
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {"x": 1}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()  # 自プロセス pid — expected_parent_pid 照合は
                            # FakeChild 側では検証しない (実 subprocess の
                            # 責務。テストは配線だけを見る)
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            # I9 対応: `finally` 節の `_ensure_dead` → `_kill` は
            # `poll() is None` の間 `proc.wait(timeout=5)` を必ず呼ぶ。
            # `wait` を持たない FakeProc だと `AttributeError` になり
            # Step 1 のテストが (正常系であっても) finally で必ず壊れる
            # (レビュー I9)。
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    # I9 対応: FakeProc.pid はテストプロセス自身の pid (上記コメント参照)。
    # `_kill`/`_escalate_kill` は `os.killpg(proc.pid, signal.SIGKILL)` を
    # 呼ぶため、`os.killpg` を monkeypatch せずに実行すると
    # **テストプロセス自身が SIGKILL される** (レビュー I9)。fake で
    # 呼び出しを記録するだけにする。
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(wr_mod.os, "killpg",
                        lambda pid, sig: killpg_calls.append((pid, sig)))

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert result.output == {"x": 1}
    assert {"role": "user", "content": "hi"} in result.transcript


def test_worker_runner_startup_timeout_kills_child(tmp_path, monkeypatch):
    """`ready` を送らないまま `worker_startup_timeout_sec` を超過させ、
    `_escalate_kill` が呼ばれ `status == "failed"` になることを確認。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        # handshake を読むが ready を返さない
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # consume handshake
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(
            update={"worker_startup_timeout_sec": 0.1})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"


def test_worker_runner_mission_timeout_escalates_sigterm_then_sigkill(
        tmp_path, monkeypatch):
    """`result` を送らないまま `mission.timeout_sec + worker_grace_sec` を
    超過させ、SIGTERM 相当の呼び出し → `worker_terminate_grace_sec` 経過後に
    SIGKILL が呼ばれることを確認。`status == "timeout"`。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        # result を送らずに待ち続ける
        child_in.read()
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)

    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(wr_mod.os, "killpg",
                        lambda pid, sig: kill_calls.append((pid, sig)))

    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(
            update={"worker_grace_sec": 0.1,
                    "worker_terminate_grace_sec": 0.1})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "timeout"
    # SIGTERM と SIGKILL が両方呼ばれることを確認
    import signal
    assert any(sig == signal.SIGTERM for _, sig in kill_calls)
    assert any(sig == signal.SIGKILL for _, sig in kill_calls)


def test_worker_runner_child_eof_before_result_is_failed(tmp_path, monkeypatch):
    """`ready` 送出後、`result` を送らずに子スレッドがパイプを閉じる (EOF)
    → `status == "failed"`。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        child_out.close()  # EOF without result

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"


def test_worker_runner_protocol_violation_is_failed(tmp_path, monkeypatch):
    """`event` フレームの `seq` を逆行させて送る → `status == "failed"`
    (fail closed — `mission_protocol.ProtocolError` を検出)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "event", "seq": 2,
                                "message": {"role": "user", "content": "hi"}})
        # seq を逆行させて送る
        write_frame(child_out, {"type": "event", "seq": 2,
                                "message": {"role": "assistant", "content": "bye"}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"


def test_worker_runner_transcript_truncates_at_cap(tmp_path, monkeypatch):
    """`transcript_max_bytes` を小さく (例: 200 バイト) 設定し、大量の
    `event` を送る → `result.transcript` に truncate marker が 1 件だけ含まれ、
    以後の `event` が積まれていないことを確認。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        # 複数の event を送る
        for i in range(2, 10):
            write_frame(child_out, {"type": "event", "seq": i,
                                    "message": {"role": "user", "content": "x" * 50}})
        write_frame(child_out, {"type": "result", "seq": 10,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(
            update={"transcript_max_bytes": 200})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    # truncate marker が正確に 1 件含まれることを確認
    truncate_markers = [m for m in result.transcript
                        if "truncated" in m.get("content", "")]
    assert len(truncate_markers) == 1
    # truncate 後のイベントが含まれていないことを確認
    truncate_idx = result.transcript.index(truncate_markers[0])
    events_after_truncate = [m for m in result.transcript[truncate_idx+1:]
                             if m.get("role") == "user"]
    assert len(events_after_truncate) == 0


def test_worker_runner_rag_rpc_dispatches_to_rag_and_responds(
        tmp_path, monkeypatch):
    """`tool_rpc` (`name="search_news"`) を送り、fake `Rag.search_news` が
    呼ばれて `tool_rpc_result` が子側パイプ (`r2`) から読めることを確認。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        rpc_result = json.loads(child_in.readline())  # tool_rpc_result
        assert rpc_result["type"] == "tool_rpc_result"
        assert rpc_result.get("ok") is True
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    rag = _rag(tmp_path)
    rag.search_news = lambda query, n=5: [{"title": "news1"}]
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock, rag=rag)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"


def test_worker_runner_rag_rpc_leak_calls_on_rpc_leak(tmp_path, monkeypatch):
    """fake `Rag.search_news` を `rpc_timeout_sec` より長くブロックする
    関数に差し替え、`on_rpc_leak` コールバックが呼ばれることを確認
    (mission 自体は `result` フレームが届けば completed のまま終わってよい —
    リーク検出とミッション結果は独立)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        # tool_rpc_result を読む (timeout でも error フレームが返ってくる)
        json.loads(child_in.readline())
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    rag = _rag(tmp_path)
    rag.search_news = lambda query, n=5: threading.Event().wait()  # 永久ハング
    settings = SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(update={"rpc_timeout_sec": 0.1})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))

    on_rpc_leak_called = []
    def on_rpc_leak_fn():
        on_rpc_leak_called.append(True)

    runner = WorkerRunner(root=root, settings=settings, clock=clock, rag=rag,
                          on_rpc_leak=on_rpc_leak_fn)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert on_rpc_leak_called, "on_rpc_leak should have been called"


def test_worker_runner_finally_joins_dispatcher_before_closing_stdin(tmp_path, monkeypatch):
    """IM-7 対応: finally 節は proc.stdin を close する前に dispatcher
    スレッドの join を完了させる (dispatcher が stdin_lock を保持して
    tool_rpc_result を書込中に close が競合するレースを防ぐ — 設計書
    §4.3「writer は 2 時点で排他」)。run() から戻った時点で dispatcher
    スレッドが確実に終了している (is_alive() is False)ことを直接検証する。
    """
    import agentic_fx.runners.worker_runner as wr_mod

    created_threads: list[threading.Thread] = []
    orig_thread_cls = wr_mod.threading.Thread

    def spying_thread(*args, **kwargs):
        t = orig_thread_cls(*args, **kwargs)
        created_threads.append(t)
        return t

    monkeypatch.setattr(wr_mod.threading, "Thread", spying_thread)

    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        json.loads(child_in.readline())  # tool_rpc_result
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    rag = _rag(tmp_path)
    rag.search_news = lambda query, n=5: []  # 即応答 (遅延自体は対象外)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock, rag=rag)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    dispatcher_threads = [th for th in created_threads
                          if th.name == "afx-worker-dispatcher"]
    assert dispatcher_threads, "dispatcher thread was not created"
    assert all(not th.is_alive() for th in dispatcher_threads)


def test_worker_runner_leaked_rag_rpc_returns_promptly_with_daemon_thread(
        tmp_path, monkeypatch):
    """FC-1 対応: RAG RPC がリークしても run() は rpc_timeout_sec 程度の
    有限時間で復帰し (ThreadPoolExecutor 版は atexit join でハングし
    得た)、生成されたワーカースレッドは daemon=True のまま残る (=
    ThreadPoolExecutor の atexit join 対象になっていない)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        # tool_rpc_result (timeout 応答) を読んでから result を送る
        json.loads(child_in.readline())
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()
    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    rag = _rag(tmp_path)
    rag.search_news = lambda query, n=5: threading.Event().wait()  # 永久リーク
    settings = SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(update={"rpc_timeout_sec": 0.2})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock, rag=rag)
    t.start()
    started = time.monotonic()
    result = runner.run(_mission())
    elapsed = time.monotonic() - started
    t.join(timeout=2.0)

    assert result.status == "completed"
    # rpc_timeout_sec=0.2 に対して十分な余裕 (dispatcher.join の上限は
    # rpc_timeout_sec + 5.0 — ThreadPoolExecutor 版なら atexit 待ちで
    # 無期限にハングし得た経路)。
    assert elapsed < 5.0
    leaked = [th for th in threading.enumerate() if th.name == "afx-rag-rpc"]
    assert leaked, "expected a leaked afx-rag-rpc thread to still be present"
    assert all(th.daemon for th in leaked)


def test_leaked_daemon_thread_does_not_block_process_exit():
    """FC-1 対応の実測統合テスト (裁定書必須項目)。実 subprocess で
    afx-rag-rpc と同じパターン (`threading.Thread(daemon=True,
    target=<永久ブロック>)`) を作った直後に `sys.exit(7)` するスクリプト
    を実行し、プロセスが実際に有限時間で終了できることを確認する。

    対比 (手元で実測確認済み — `ThreadPoolExecutor` 版の再現):
    `concurrent.futures.ThreadPoolExecutor(max_workers=1)` に `submit`
    した `threading.Event().wait()` を `shutdown(wait=False)` した後に
    `sys.exit(...)` する同型スクリプトは、atexit ハンドラ
    (`concurrent.futures.thread._python_exit`) がワーカースレッドの
    終了を待つため `subprocess.run(..., timeout=5)` が
    `TimeoutExpired` になる (rc=124 相当)。本テストは `daemon=True` の
    素の `threading.Thread` に置き換えたことで、このハングが解消される
    ことを実行結果で固定する。
    """
    script = (
        "import threading, sys\n"
        "threading.Thread(target=lambda: threading.Event().wait(), "
        "daemon=True, name='afx-rag-rpc').start()\n"
        "sys.exit(7)\n")
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 7


def test_real_subprocess_starts_and_reports_ready_then_times_out(tmp_path):
    """実 subprocess を spawn する最小 E2E。llama-swap への接続先が存在
    しない (base_url を到達不能な値に上書き) ため LocalRunner 側は
    timeout する — 子プロセスの起動・handshake・ready 応答・preemption
    による終了までが実際に動くことを実証する (詳細なハング注入・
    kill 検証は Task 20 の E2E に譲る)。
    """
    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "llama_swap": SETTINGS.llama_swap.model_copy(
            update={"base_url": "http://127.0.0.1:1", "timeout_sec": 2}),
        "worker": SETTINGS.worker.model_copy(
            update={"worker_grace_sec": 2.0, "worker_terminate_grace_sec": 2.0,
                    "worker_startup_timeout_sec": 15.0})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path))
    mission = Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=2.0)
    result = runner.run(mission)
    assert result.status in ("timeout", "failed")


# ---------------------------------------------------------------------------
# 指揮者検証 + 1 周目レビューで見つかった「防御はあるがテストが素通りする」
# 6 件への回帰ピン (2026-08-08)。
#
# 内訳: 指揮者の変異 14 件で生存した 5 件 (S1〜S5) と、sonnet 副査が単独で
# 検出した 1 件 (tool_rpc_result の seq)。ローカル LLM (qwen3-coder-30b) は
# S1〜S5 を 5/5 で的中させた。
# ---------------------------------------------------------------------------


def _tiny_worker_settings(**over):
    """worker 設定を短縮した Settings を作る (テストを速くするため)。"""
    base = dict(worker_startup_timeout_sec=0.2, worker_grace_sec=0.1,
                worker_terminate_grace_sec=0.3, rpc_timeout_sec=0.2)
    base.update(over)
    return SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(update=base)})


def _spawn_runner(monkeypatch, tmp_path, fake_proc, settings, *, rag=None,
                  on_rpc_leak=None, close_on_kill=None, stop_event=None):
    """`subprocess.Popen` と `os.killpg` を差し替えて WorkerRunner を作り、
    `killpg` の呼び出し (pid, signal, 時刻) を記録するリストを返す。"""
    import agentic_fx.runners.worker_runner as wr_mod

    kill_calls: list[tuple] = []
    popen_kwargs: dict = {}

    def fake_popen(*a, **k):
        popen_kwargs.update(k)
        return fake_proc

    monkeypatch.setattr(wr_mod.subprocess, "Popen", fake_popen)

    def fake_killpg(pid, sig):
        kill_calls.append((pid, sig, time.monotonic()))
        # **SIGKILL は子を実際に死なせる** — 本番では子が死ぬとパイプが
        # 閉じ、親の reader が EOF を受けて `finally` を抜けられる。
        # fake がこれを模さないと、親は `proc.stdout.close()` で
        # (readline 中の reader がバッファロックを保持しているため)
        # 永久にブロックする。実測で判明した必須の忠実性。
        #
        # **生の `os.close()` は使わない (2026-08-09 Task 19 で実測した
        # flaky の発生源)。** 子スレッド側は同じ fd を
        # `child_out = os.fdopen(w, "wb")` でラップして**所有**している。
        # ここで生 close すると同じ fd に所有者が 2 つできてしまい、先に
        # こちらが閉じた後で `child_out` が (スレッド終了時の参照カウント
        # 減で) finalize されると `OSError: [Errno 9] Bad file descriptor`
        # が finalizer から送出され、`PytestUnraisableExceptionWarning`
        # になる (main で 8/30 の頻度で再現)。
        #
        # `/dev/null` を `dup2` で被せると、①パイプの write 端は解放される
        # ので親の reader は EOF を受け取れる (上記の忠実性は維持) ②fd 番号
        # 自体は有効なまま残るので `child_out.close()` は正常に成功する
        # (所有者が実質 1 つになる)。
        if sig == signal.SIGKILL and close_on_kill is not None:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, close_on_kill)
                finally:
                    os.close(devnull)
            except OSError:
                pass

    monkeypatch.setattr(wr_mod.os, "killpg", fake_killpg)
    runner = WorkerRunner(root=_root(tmp_path), settings=settings,
                          clock=FixedClock(datetime(2026, 8, 4,
                                                    tzinfo=timezone.utc)),
                          rag=rag if rag is not None else _rag(tmp_path),
                          on_rpc_leak=on_rpc_leak, stop_event=stop_event)
    return runner, kill_calls, popen_kwargs


def _silent_child_proc(tmp_path):
    """`ready` を一切送らない子。

    **handshake は必ず読み出す** — 読まないと親の `write_frame` が
    パイプバッファで詰まってテストがハングする (既存テストの
    `child_thread_fn` が同じ理由で handshake を consume している)。
    `poll()` が常に `None` = SIGTERM を無視し続ける子を模す。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()

    # レビュー 2 周目 (sonnet Minor): ローカル変数のままだと関数を抜けた
    # 時点で refcount が 0 になり、**CPython は fd を閉じてしまう**
    # (「閉じない」というコメントが実装で保証されていなかった)。
    # 親の後続 write が BrokenPipe にならないよう、明示的に参照を保持する。
    kept: list = []

    def consume_handshake():
        child_in = os.fdopen(r2, "rb")
        kept.append(child_in)        # 参照を保持して GC による close を防ぐ
        child_in.readline()          # handshake を読み捨てる

    th = threading.Thread(target=consume_handshake, daemon=True)
    th.start()

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None          # 常に生存 = SIGTERM を無視する子

        def wait(self, timeout=None):
            return -9

    return FakeProc(), w, th


def test_startup_timeout_actually_kills_the_child(tmp_path, monkeypatch):
    """**S1 系 / sonnet I-1**: startup timeout で子が**実際に kill される**。

    元のテストは `os.killpg` を記録なしの no-op に差し替えていたため、
    `_escalate_kill(proc, w)` の**呼び出しごと削除しても緑**だった
    (sonnet 副査が実測)。ここでは `killpg` の呼び出しを記録して pin する。
    """
    settings = _tiny_worker_settings()
    proc, w, _th = _silent_child_proc(tmp_path)
    runner, kill_calls, _ = _spawn_runner(monkeypatch, tmp_path, proc, settings,
                                          close_on_kill=w)
    try:
        result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                    max_turns=1, timeout_sec=5.0))
        assert result.status == "failed"
        assert kill_calls, "startup timeout なのに子を kill していない"
        assert kill_calls[0][1] == signal.SIGTERM
    finally:
        try:
            os.close(w)          # SIGKILL 時に閉じ済みなら OSError で無視
        except OSError:
            pass


def test_escalation_waits_the_configured_grace_between_sigterm_and_sigkill(
        tmp_path, monkeypatch):
    """**S1**: SIGTERM と SIGKILL の**間に `worker_terminate_grace_sec` 待つ**。

    元のテストは「SIGTERM と SIGKILL がそれぞれ呼ばれた」ことしか見ておらず、
    **grace 待ちを 0 にする変異が生存**していた (指揮者が実測。sonnet・
    qwen3-coder-30b・glm-4.7-flash・gemma-4-31b がいずれも指摘)。
    子は `poll()` が常に `None` = SIGTERM を無視するので、実装は必ず
    grace を待ってから SIGKILL に上げるはず。**呼び出し時刻の差**を見る。
    """
    grace = 0.4
    settings = _tiny_worker_settings(worker_terminate_grace_sec=grace)
    proc, w, _th = _silent_child_proc(tmp_path)
    runner, kill_calls, _ = _spawn_runner(monkeypatch, tmp_path, proc, settings,
                                          close_on_kill=w)
    try:
        runner.run(Mission(prompt="p", tools=[], output_schema={},
                           max_turns=1, timeout_sec=5.0))
        sigs = [(s, t) for _pid, s, t in kill_calls]
        assert [s for s, _ in sigs][:2] == [signal.SIGTERM, signal.SIGKILL], \
            f"SIGTERM → SIGKILL の順で呼ばれていない: {sigs}"
        waited = sigs[1][1] - sigs[0][1]
        # 遅延は経過を伸ばす方向にしか働かないので下限で pin できる。
        assert waited >= grace * 0.8, (
            f"grace ({grace}s) を待たずに SIGKILL へ上げている "
            f"(実測 {waited:.3f}s)")
    finally:
        try:
            os.close(w)          # SIGKILL 時に閉じ済みなら OSError で無視
        except OSError:
            pass


def test_mission_deadline_includes_worker_grace_sec(tmp_path, monkeypatch):
    """**S3**: preemption の予算が `mission.timeout_sec + worker_grace_sec`。

    `+ w.worker_grace_sec` を落とす変異が生存していた (指揮者が実測、
    qwen3-coder-30b が単独で指摘)。`timeout_sec` を極小 (0.05) に、
    `worker_grace_sec` を相対的に大きく (0.5) 取り、**grace を足していれば
    まだ timeout していない**時刻に結果が出ないことで判別する。
    """
    # **`worker_terminate_grace_sec` を極小にする** (レビュー 2 周目 sonnet):
    # `_escalate_kill` の grace は timeout 検出**後**に走るので `elapsed` に
    # 加算される。`_tiny_worker_settings` の既定 0.3 が乗ると、grace を
    # 落とす変異でも `elapsed ≈ 0.05 + 0.3 = 0.351s` になり、閾値 0.4 との
    # 差はわずか 0.05s しかなかった (実測)。**このテストと無関係な既定値が
    # マージンを作っている**状態で、他テストの都合で 0.3 → 0.4 に変われば
    # この pin は静かに死ぬ (Task 9 の「水増し」と同じ故障クラス)。
    # 極小にすれば変異時 `elapsed ≈ 0.06`、正常時 `≈ 0.56` となり、
    # 閾値 0.4 は両者の中間で十分なマージンを持つ。
    settings = _tiny_worker_settings(worker_grace_sec=0.5,
                                     worker_startup_timeout_sec=2.0,
                                     worker_terminate_grace_sec=0.01)
    r, w = os.pipe()
    r2, w2 = os.pipe()

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    proc = FakeProc()
    runner, kill_calls, _ = _spawn_runner(monkeypatch, tmp_path, proc, settings,
                                          close_on_kill=w)

    child_w = os.fdopen(w, "wb")

    def child():
        write_frame(child_w, {"type": "ready", "seq": 1, "ok": True})
        # result は送らない → 親は deadline_budget で打ち切るはず

    threading.Thread(target=child, daemon=True).start()
    try:
        started = time.monotonic()
        result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                    max_turns=1, timeout_sec=0.05))
        elapsed = time.monotonic() - started
        assert result.status == "timeout"
        # grace (0.5) を足していれば 0.05 では打ち切れない。
        assert elapsed >= 0.4, (
            "worker_grace_sec を予算に足していない "
            f"(timeout_sec=0.05 に対し実測 {elapsed:.3f}s)")
    finally:
        for f in (child_w,):
            try:
                f.close()
            except OSError:
                pass
        try:
            os.close(r2)
        except OSError:
            pass


def test_popen_uses_start_new_session_so_killpg_targets_the_child_group(
        tmp_path, monkeypatch):
    """**S4**: `start_new_session=True` が `Popen` に渡ること。

    これを外すと `os.killpg` が**親のプロセスグループ**を対象にしてしまう
    (`FakeProc.pid` は自プロセス pid なので、テストで `killpg` を実物に
    したらテストプロセス自身が死ぬ)。全テストが `killpg` を monkeypatch
    しているため**実プロセスでしか露見しない**穴だった。
    指揮者の自主変異で発見し、ローカル LLM 4 モデル中 3 つが Critical と
    格付けした。**渡された kwargs を直接 pin する**のが唯一の確実な方法。
    """
    settings = _tiny_worker_settings()
    proc, w, _th = _silent_child_proc(tmp_path)
    runner, _kill, popen_kwargs = _spawn_runner(monkeypatch, tmp_path, proc,
                                                settings, close_on_kill=w)
    try:
        runner.run(Mission(prompt="p", tools=[], output_schema={},
                           max_turns=1, timeout_sec=5.0))
        assert popen_kwargs.get("start_new_session") is True, (
            "start_new_session=True が Popen に渡っていない — os.killpg が "
            "親のプロセスグループを殺す")
    finally:
        try:
            os.close(w)          # SIGKILL 時に閉じ済みなら OSError で無視
        except OSError:
            pass


def test_protocol_violation_is_detected_by_seq_check_not_by_eof(
        tmp_path, monkeypatch):
    """**S2**: seq 逆行が **`in_seq.check` によって** 検出されること。

    既存の `test_worker_runner_protocol_violation_is_failed` は、子が
    最後に `child_out.close()` して **EOF を送る**ため、`in_seq.check` を
    削除しても「EOF → failed」の経路で緑になっていた (指揮者の変異で実測。
    ローカル LLM の qwen3-coder-30b が「`in_seq.check()` が呼ばれたことを
    検証すべき」と修正案まで的中させた)。

    ここでは **子は閉じず、逆行フレームの後に正常な `result` を送る**。
    seq 検証が生きていれば、その `result` に到達する前に `failed` で
    終わるはず。検証を外すと `result` が採用されて `completed` になる。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())                      # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "event", "seq": 2,
                                "message": {"role": "user", "content": "hi"}})
        write_frame(child_out, {"type": "event", "seq": 2,   # ← 逆行
                                "message": {"role": "a", "content": "bye"}})
        # **閉じない**。seq 検証が無ければ親はこの result を採用してしまう。
        write_frame(child_out, {"type": "result", "seq": 4,
                                "status": "completed", "output": {"ok": 1}})

    th = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    proc = FakeProc()
    runner, _kill, _kw = _spawn_runner(monkeypatch, tmp_path, proc,
                                       _tiny_worker_settings(),
                                       close_on_kill=w)
    th.start()
    result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                max_turns=1, timeout_sec=5.0))
    th.join(timeout=2.0)

    assert result.status == "failed", (
        "seq 逆行が検出されず、後続の result が採用されている "
        "(in_seq.check が効いていない)")


def test_tool_rpc_result_seq_starts_at_2_after_handshake(tmp_path, monkeypatch):
    """**sonnet 副査の単独検出**: 親が返す最初の `tool_rpc_result` の
    `seq` は **2**。

    Task 7 のレビュー 3 周目で codex が明文化した契約 —— 親→子方向の
    `SeqTracker` は **handshake が seq=1 を消費**するため、最初の
    `tool_rpc_result` は 2 から始まらなければならない。子側
    (`mission_worker._RagRpcProxy`) は `in_seq` を handshake と共有して
    検証するので、ここがずれると **RAG を使う Mission が初回呼出しで必ず
    `ProtocolError`** になる。

    実測: `seq` の計算を 1 ずらす変異が **全 1549 テストを素通り**した。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()
    seen: list[dict] = []

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())                      # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "usdjpy", "n": 5}})
        seen.append(json.loads(child_in.readline()))         # tool_rpc_result
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {"ok": 1}})

    th = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    proc = FakeProc()
    rag = _rag(tmp_path)
    runner, _kill, _kw = _spawn_runner(monkeypatch, tmp_path, proc,
                                       _tiny_worker_settings(rpc_timeout_sec=5.0),
                                       rag=rag, close_on_kill=w)
    th.start()
    runner.run(Mission(prompt="p", tools=[], output_schema={},
                       max_turns=1, timeout_sec=5.0))
    th.join(timeout=3.0)

    assert seen, "tool_rpc_result が返ってこなかった"
    assert seen[0]["type"] == "tool_rpc_result"
    assert seen[0]["seq"] == 2, (
        "最初の tool_rpc_result の seq が 2 でない — 子の in_seq は "
        f"handshake で 1 を消費済みなので ProtocolError になる: {seen[0]}")


def test_stdin_is_closed_only_after_the_dispatcher_finished_writing(
        tmp_path, monkeypatch):
    """**S5**: `finally` は **dispatcher の完了を待ってから** stdin を閉じる。

    既存の `test_worker_runner_finally_joins_dispatcher_before_closing_stdin`
    は run() 復帰後に `is_alive() is False` を見るだけで、**終了状態**しか
    検証していない。dispatcher は `dispatch_queue.put(None)` の sentinel でも
    自然終了するため、`dispatcher.join(...)` を削除しても大抵は先に終わって
    しまい **変異が生存**した (指揮者が実測)。

    ここでは **dispatcher をわざと遅くする** (RAG 呼び出しを 0.4 秒
    ブロックさせる)。`result` は即座に届くので、親は dispatcher が
    まだ応答を書いている最中に `finally` へ入る。join があれば
    「RPC 応答の書込完了 → stdin close」の順、無ければ逆順になる。
    IM-7 が守ろうとしている「writer は 2 時点で排他」そのものの pin。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()
    order: list[str] = []

    class SlowRag:
        def search_news(self, query, n=5):
            time.sleep(0.4)
            order.append("rpc_computed")
            return [{"title": "t", "body": "b", "source_name": "s"}]

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())                      # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        # RPC の応答を待たずに result を送る → 親は dispatcher 実行中に
        # finally へ入る。
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {"ok": 1}})

    th = threading.Thread(target=child_thread_fn, daemon=True)

    real_stdin = os.fdopen(w2, "wb")

    class SpyStdin:
        """`close()` の順序だけを記録し、他は実体へ委譲する。"""

        def __init__(self, inner):
            self._inner = inner

        def write(self, data):
            return self._inner.write(data)

        def flush(self):
            return self._inner.flush()

        def close(self):
            order.append("stdin_closed")
            return self._inner.close()

    class FakeProc:
        pid = os.getpid()
        stdin = SpyStdin(real_stdin)
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    proc = FakeProc()
    runner, _kill, _kw = _spawn_runner(
        monkeypatch, tmp_path, proc,
        _tiny_worker_settings(rpc_timeout_sec=5.0),
        rag=SlowRag(), close_on_kill=w)
    th.start()
    result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                max_turns=1, timeout_sec=5.0))
    th.join(timeout=3.0)

    assert result.status == "completed"
    # レビュー 2 周目 (sonnet Minor): 変異時は `rpc_computed` が **order に
    # 現れない**ため、旧版は手前の `assert "rpc_computed" in order` で落ちて
    # いた。捕まえてはいるが**意図と違う assert で落ちる**ので、将来の
    # 読み手を誤導する。順序そのものを 1 つの assert で表現する。
    assert order == ["rpc_computed", "stdin_closed"], (
        "dispatcher の完了を待たずに stdin を閉じている "
        f"(IM-7 の排他契約違反)。期待 ['rpc_computed', 'stdin_closed'] / "
        f"実際 {order}")


def test_dispatcher_never_writes_to_stdin_after_it_was_closed(
        tmp_path, monkeypatch):
    """プラン8 Task 19: `dispatcher.join` がタイムアウトして生き残った
    dispatcher は、close 済みの `proc.stdin` へ**書かない**。

    `_run_with_child` の `finally` は `dispatcher.join(timeout=
    rpc_timeout_sec + 5.0)` と**有界**なので (FC-1: dispatcher が無限に
    ブロックしてもプロセス終了を妨げないため)、dispatcher が join を
    生き延びる経路が構造的に存在する。その dispatcher が後から
    `write_frame(proc.stdin, ...)` を実行すると、閉じた fd への書き込みに
    なる。`stdin_state["closed"]` のガードはこれを防ぐためにある。

    **このピンが無いと、ガードを消しても値の設定を消しても全件緑になる**
    (2026-08-09 指揮者が変異 2 種を全件 1706 passed で生存させて実測)。

    仕掛け: 子は tool_rpc を 2 本連続で送ってから result を送る。親の
    dispatcher は 1 本目の応答書き込みで `stdin_lock` を保持したまま
    join budget を超えて眠る。main はその間に join をタイムアウトさせ、
    lock が空くのを待ってから `closed` を立てて stdin を閉じる。
    dispatcher は 2 本目の応答でガードに当たる — ここで書けば red。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()

    # join budget = rpc_timeout_sec + 5.0 = 5.05s。1 本目の書き込みは
    # これを超えて眠る必要がある (超えないと dispatcher は join 内で
    # 終了してしまい、close 後の経路がそもそも実行されない)。
    settings = _tiny_worker_settings(rpc_timeout_sec=0.05)
    first_write_sleep_sec = 5.5

    rag_calls: list[str] = []

    class CountingRag:
        def search_news(self, query, n=5):
            rag_calls.append(query)
            return [{"title": "t", "body": "b", "source_name": "s"}]

    # **子の読み取り端はテスト本体で保持する。** 子スレッド内のローカルに
    # すると、スレッド終了時の参照カウント減で `r2` が閉じられ、その後の
    # dispatcher の応答書き込み (flush) が `BrokenPipeError` になって
    # `except (BrokenPipeError, OSError): return` へ落ちる。すると
    # dispatcher が 2 本目の tool_rpc に到達せず、このテストが検証したい
    # 「close 後の書き込み経路」が実行されない (2026-08-09 実測で判明)。
    child_in = os.fdopen(r2, "rb")

    def child_thread_fn():
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())                      # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q1", "n": 5}})
        write_frame(child_out, {"type": "tool_rpc", "seq": 3, "rpc_id": "2",
                                "name": "search_news",
                                "args": {"query": "q2", "n": 5}})
        # 応答を待たずに result を送る → 親は dispatcher 実行中に finally へ。
        write_frame(child_out, {"type": "result", "seq": 4,
                                "status": "completed", "output": {"ok": 1}})
        child_out.close()   # EOF — 親の reader.join を待たせない

    th = threading.Thread(target=child_thread_fn, daemon=True)

    real_stdin = os.fdopen(w2, "wb")

    class SpyStdin:
        """書き込みを記録し、`close()` の前後を区別する。"""

        def __init__(self, inner):
            self._inner = inner
            self.closed_flag = False
            self.writes_before_close = 0
            self.writes_after_close = 0
            self._first_rpc_result_done = False

        def write(self, data):
            if self.closed_flag:
                self.writes_after_close += 1
                return len(data)
            self.writes_before_close += 1
            written = self._inner.write(data)
            # 1 本目の tool_rpc_result 応答だけ、join budget を超えて眠る。
            # handshake (親が最初に書く 1 本) は対象外。
            if (not self._first_rpc_result_done
                    and self.writes_before_close > 1):
                self._first_rpc_result_done = True
                time.sleep(first_write_sleep_sec)
            return written

        def flush(self):
            if self.closed_flag:
                return None
            return self._inner.flush()

        def close(self):
            self.closed_flag = True
            return self._inner.close()

    spy = SpyStdin(real_stdin)

    class FakeProc:
        pid = os.getpid()
        stdin = spy
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    proc = FakeProc()
    runner, _kill, _kw = _spawn_runner(
        monkeypatch, tmp_path, proc, settings,
        rag=CountingRag(), close_on_kill=w)
    th.start()
    result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                max_turns=1, timeout_sec=5.0))
    th.join(timeout=3.0)

    assert result.status == "completed"

    # dispatcher は daemon なので、2 本目の応答経路を通り終えるのを待つ。
    deadline = time.monotonic() + 5.0
    while len(rag_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    # **恒真化の防止**: 2 本目の RPC が close の**後**に計算されたことを
    # 確かめる。これが無いと「dispatcher が close 前に全部終えていたので
    # 書き込みが無かっただけ」でも緑になり、ガードを何も守らない。
    assert spy.closed_flag is True, "stdin が閉じられていない (前提が崩れている)"
    assert len(rag_calls) == 2, (
        "dispatcher が 2 本目の tool_rpc に到達していない — このテストは "
        f"close 後の書き込み経路を検証できていない (rag_calls={rag_calls}, "
        f"writes_before={spy.writes_before_close}, "
        f"writes_after={spy.writes_after_close}, "
        f"slept={spy._first_rpc_result_done})")

    assert spy.writes_after_close == 0, (
        "close 済みの stdin へ dispatcher が書き込んだ "
        "(stdin_state['closed'] ガードが機能していない)")


def _hanging_child(*, send_ready: bool):
    """`ready` を送る/送らないだけを選べる、result を永久に返さない子。

    パイプは閉じない (= 生きている子)。SIGKILL の fake が `close_on_kill`
    で write 端を解放するまで親の reader は EOF を受け取らない。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()
    child_in = os.fdopen(r2, "rb")   # 保持しないと親の書き込みが EPIPE になる

    def child_thread_fn():
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())                      # handshake
        if send_ready:
            write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        # result は送らない — 親を待たせ続ける。
        while True:
            time.sleep(0.05)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    return (threading.Thread(target=child_thread_fn, daemon=True),
            FakeProc(), w, child_in)


@pytest.mark.parametrize(
    "send_ready,expected_status,phase",
    [(True, "timeout", "result 待ち"), (False, "failed", "ready 待ち")])
def test_stop_event_interrupts_the_wait_immediately(
        tmp_path, monkeypatch, send_ready, expected_status, phase):
    """プラン8 Task 19: `stop_event` が立ったら待ちを**即座に**打ち切る。

    設計書 §5 手順1「同時に実行中 worker へ SIGTERM を発行 (3 と並行開始)」
    — 停止シーケンスは実行中 Mission の自然完了を待たない。`_wait_with_stop`
    はこのためにあり、`stop_event` を見ないと Mission の
    `timeout_sec + worker_grace_sec` (既定 300 秒超) を待ち切ってしまう。

    **このピンが無いと `stop_event` の配線 (build_app が同一 Event を渡して
    いること) は緑でも、「立てたときに実際に中断できるか」は一度も検証され
    ない** — `_wait_with_stop` はテストから一度も叩かれていなかった
    (2026-08-09 指揮者が実測)。

    停止で打ち切ったときの `MissionResult` の status は、`ready` 待ちなら
    `"failed"`、`result` 待ちなら `"timeout"` になる (= 通常の待ち超過と
    同じ扱い)。**これは in-band 契約 (kill は `status="timeout"` で返す、
    設計書 §4.1) に沿った意図的な裁定**であり、停止専用の第 5 の status は
    設けない (`MissionResult` の 4 値契約は本プランで不変)。
    """
    th, proc, w, _child_in = _hanging_child(send_ready=send_ready)
    stop_event = threading.Event()

    # Mission 側の予算は十分に長くする — stop_event を見ていなければ
    # この待ちで確実にタイムアウト判定より先に時間切れになる。
    settings = _tiny_worker_settings(worker_startup_timeout_sec=30.0)
    runner, _kill, _kw = _spawn_runner(
        monkeypatch, tmp_path, proc, settings, close_on_kill=w,
        stop_event=stop_event)

    th.start()
    threading.Timer(0.3, stop_event.set).start()

    started = time.monotonic()
    result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                max_turns=1, timeout_sec=30.0))
    elapsed = time.monotonic() - started

    assert result.status == expected_status
    # 30 秒の予算を待ち切っていないこと (中断が効いている)。
    assert elapsed < 5.0, (
        f"{phase} で stop_event による中断が効いていない "
        f"(elapsed={elapsed:.2f}s — 予算 30s を待ち切った疑い)")


def _capture_handshake(base, monkeypatch, *, worker_profile):
    """WorkerRunner を 1 回走らせ、親が子へ送った handshake を返す。

    子は handshake を読んだら即 `ready: ok=False` を返して終わる (Mission
    本体は走らせない — 見たいのは handshake の中身だけ)。
    """
    r, w = os.pipe()
    r2, w2 = os.pipe()
    captured: dict = {}

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        captured.update(json.loads(child_in.readline()))
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": False})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    base.mkdir(parents=True, exist_ok=True)
    root = _root(base)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(base), worker_profile=worker_profile)
    t.start()
    runner.run(_mission())
    t.join(timeout=2.0)
    return captured


def test_improve_profile_handshake_omits_db_and_plugin_paths(tmp_path, monkeypatch):
    """**構造的到達不能の防御層① の回帰ピン** (プラン8 Task 18, 設計書 §4.6)。

    improve worker への handshake には `db_path`/`plugins_dir` を**入れない**
    — これが「接続情報の非提供」そのもので、Landlock (層②) と合わせて
    2 層防御を成す。

    根拠 (指揮者の段0 変異スイープ M9 で実測): この条件分岐を外して
    improve の子にも実パスを渡すようにしても、**フルスイート 1682 件が
    全て green のままだった**。層① はコードの見た目だけで守られており、
    テストが一切押さえていなかった。

    **両方向を見る** (片側だけだと恒真に落ちる — mutation-testing の規律):
    improve では `None`、trade では実パス。
    """
    # `_root()` は同じ tmp_path 上で 2 回作れない (FileExistsError) ので
    # 呼び出しごとに別ディレクトリを渡す。
    improve = _capture_handshake(tmp_path / "a", monkeypatch,
                                 worker_profile="improve")
    assert improve["worker_profile"] == "improve"
    assert improve["db_path"] is None, (
        "improve worker に DB パスを渡している — 防御層①(接続情報の非提供)"
        "が壊れている (設計書 §4.6)")
    assert improve["plugins_dir"] is None, (
        "improve worker に plugins_dir を渡している — 同上")

    trade = _capture_handshake(tmp_path / "b", monkeypatch,
                               worker_profile="trade")
    assert trade["worker_profile"] == "trade"
    assert trade["db_path"] is not None and trade["db_path"].endswith(
        "agentic.db"), "trade worker には DB パスが渡らなければならない"
    assert trade["plugins_dir"] is not None


def test_child_cwd_is_a_dedicated_dir_outside_the_repository(tmp_path, monkeypatch):
    """**`Popen(cwd=...)` は防御層② の一部** (プラン8 Task 18, codex 1周目 #1)。

    子の cwd は `mission_worker._bootstrap_improve_profile` がそのまま
    **read-write** allowlist に入れる。`cwd=` を落とすと子は親の cwd
    (= リポジトリ root) を継承し、`data/` が書込可能になる。

    段0 変異スイープで実測: `cwd=workdir` を削除してもフルスイート 1683 件が
    全 green だった。子側には fail-closed ガードを入れたが、**親が正しい
    cwd を渡していること自体**もここで直接押さえる (子のガードは最後の砦で
    あって、親の配線の代わりにはならない)。
    """
    captured: dict = {}

    class FakeProc:
        pid = os.getpid()
        stdin = None
        stdout = None
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        # workdir は `tempfile.TemporaryDirectory` の context を抜けた時点で
        # 消えるので、「`home`/`tmp`/`cfg` の 3 つ以外が無いこと」はここ (子の起動時点) で見る。
        if "cwd" in kwargs:
            captured["cwd_entries"] = sorted(p.name for p in
                                             Path(kwargs["cwd"]).iterdir())
        raise RuntimeError("stop here — 見たいのは Popen の引数だけ")

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path), worker_profile="improve")
    with pytest.raises(RuntimeError, match="stop here"):
        runner.run(_mission())

    assert "cwd" in captured, (
        "Popen に cwd= を渡していない — 子がリポジトリ root を継承し、"
        "improve worker の read-write allowlist に data/ が入る")
    cwd = Path(captured["cwd"]).resolve()
    data_dir = (root / "data").resolve()
    assert cwd != data_dir and cwd not in data_dir.parents, (
        f"子の cwd ({cwd}) が data/ ({data_dir}) を覆っている")
    assert captured["cwd_entries"] == ["cfg", "home", "tmp"], (
        f"子の workdir は {{'cfg', 'home', 'tmp'}} 以外を含んではならない "
        f"(実際: {captured['cwd_entries']})")


def test_worker_runner_reads_reason_from_result_frame(tmp_path, monkeypatch):
    """Task 3 / CP11: 子が result フレームに載せた reason が
    MissionResult.reason まで届く。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "failed", "output": None,
                                "reason": "context exceeded: prompt 1 "
                                          "tokens > n_ctx 2 (model=m)"})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"
    assert result.reason == (
        "context exceeded: prompt 1 tokens > n_ctx 2 (model=m)")


def test_worker_runner_tolerates_unknown_keys_in_result_frame(
        tmp_path, monkeypatch):
    """Task 3 / CP12 (codex M2): result フレームに reason に加えて未知
    キーが混ざっても reader は落ちず、reason を含む既知キーだけを使って
    MissionResult を組み立てる (将来のフレーム拡張に対する前方互換の
    回帰固定 — `mission_protocol.read_frame` は dict であることと seq
    しか検証しないことを実コードで確認済み)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "completed", "output": {"x": 1},
                                "reason": "some reason",
                                "future_field_not_yet_defined": "ignore me"})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert result.output == {"x": 1}
    assert result.reason == "some reason"


def test_worker_runner_reason_defaults_to_none_for_legacy_result_frame(
        tmp_path, monkeypatch):
    """Task 3 / 1 周目の生存変異 X1: `reason` キーを持たない result
    フレーム (例外パスの `error` フレーム・プラン 8 以前の子) では
    `MissionResult.reason` が **None のまま**であることを固定する。

    `payload.get("reason")` を `payload.get("reason", "")` に緩める変異は
    既存 9 件の後方互換テストをすべて素通りする (指揮者が実測)。既存
    テストは reason を assert しないため、空文字が入り込んでも気づけない。
    Task 1 の契約は「reason の既定は None」であり、`is None` で診断の
    有無を判定する呼び出し側はこの差で壊れる。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        # mission_worker の例外パスが送る形 (reason は無く error を持つ)。
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "failed", "output": None,
                                "error": "RuntimeError: boom"})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"
    assert result.reason is None


def test_worker_runner_preserves_empty_string_reason(tmp_path, monkeypatch):
    """Task 3 / 1 周目 codex 指摘 M8: 子が **明示的に** `"reason": ""` を
    送ってきたら、親はそれを空文字のまま `MissionResult.reason` に載せる。

    `payload.get("reason")` を `payload.get("reason") or None` に緩める変異
    (空文字を None へ潰す) は既存テスト全件を素通りする — 受信側テストが
    非空文字列とキー欠落しか渡していないため (指揮者が実測: 1762 passed の
    まま)。「キーが無い (診断なし → None)」と「キーはあるが空 (子が空の
    診断を送った → プロトコル違反の兆候)」は別の事象であり、**プロトコル層
    は子が送った値をそのまま親へ渡す**。

    ⚠️ これは**表示層の契約ではない**。Task 4 の trade/reflection 出口は
    `if result.reason` で真値判定するため、空文字は表示・通知の上では
    None と同じく「reason 無し」に丸められる (その丸めは表示層の判断で
    あって、プロトコル層が先回りして潰してよい理由にはならない)。
    2 つの層が食い違って見えても、それは意図された役割分担である。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "failed", "output": None,
                                "reason": ""})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"
    assert result.reason == ""
    assert result.reason is not None


# Step 31: WorkerRunner 親側拡張テスト
_READY_CHILD_SCRIPT = (
    "import sys, json\n"
    "sys.stdin.readline()\n"
    "print(json.dumps({'type':'ready','seq':1,'ok':True}))\n"
    "sys.stdout.flush()\n"
)


def test_worker_runner_home_tmp_cfg_subdirs_are_mode_0700(monkeypatch, tmp_path):
    """親側 protocol sequence 手順 1: `home`/`tmp`/`cfg` を 0700 で作る。

    **B8 の再発防止**: workdir 自体 (`tempfile.TemporaryDirectory` 由来) は
    Python が既定で 0700 を作るため、workdir 自身の mode を見る旧テストは
    `workdir.chmod(0o700)` を削除しても恒真 (green のまま) だった。観測点を
    「本 task が明示 `mkdir(mode=0o700)` で作る」 `home`/`tmp`/`cfg` の
    3 subdir に移す。"""
    import stat
    captured: dict = {}
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        cwd = Path(kw["cwd"])
        captured["modes"] = {
            name: stat.S_IMODE(os.stat(cwd / name).st_mode)
            for name in ("home", "tmp", "cfg")}
        return orig_popen([sys.executable, "-c", _READY_CHILD_SCRIPT], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="improve")
    runner.run(_mission())
    assert captured["modes"] == {"home": 0o700, "tmp": 0o700, "cfg": 0o700}


def test_worker_runner_creates_home_tmp_cfg_subdirs(monkeypatch, tmp_path):
    """親側 protocol sequence 手順 1: workdir 直下に home/tmp/cfg を作る
    (存在の検査 — mode の検査は上記と分離、B7 の再発防止で spy 内側観測)。"""
    captured: dict = {}
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        cwd = Path(kw["cwd"])
        captured["is_dir"] = {name: (cwd / name).is_dir()
                              for name in ("home", "tmp", "cfg")}
        return orig_popen([sys.executable, "-c", _READY_CHILD_SCRIPT], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(root=root,
                          settings=_worker_settings(claude_backend=True),
                          clock=FixedClock(NOW), rag=_rag(tmp_path),
                          worker_profile="improve")
    runner.run(_mission())
    assert captured["is_dir"] == {"home": True, "tmp": True, "cfg": True}


def test_worker_runner_copies_claude_credentials_before_spawn(monkeypatch, tmp_path):
    """親側 protocol sequence 手順: auth copy は spawn より前 (通常ファイル・
    所有者・mode・サイズ検査つき)。"""
    creds = tmp_path / "creds" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text('{"token":"x"}')
    creds.chmod(0o600)
    captured: dict = {}
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        cfg_dir = Path(kw["cwd"]) / "cfg"
        captured["cfg_has_creds"] = (cfg_dir / ".credentials.json").is_file()
        return orig_popen([sys.executable, "-c", _READY_CHILD_SCRIPT], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(
        root=root,
        settings=_worker_settings(claude_backend=True, credentials_file=str(creds)),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve")
    runner.run(_mission())
    assert captured["cfg_has_creds"] is True


def test_worker_runner_does_not_copy_auth_json_for_codex_llama_swap(
        monkeypatch, tmp_path):
    """§7.1-2: `codex + provider=llama_swap` は ChatGPT サブスクの資格情報
    (`auth.json`) をコピーせず、空の scratch `CODEX_HOME` で起動する
    (指揮者検収 B2 — Task 1 差し戻し。base `dacb41d` の
    `worker_runner.py` は既に `provider == "chatgpt"` で分岐しており
    production は正しいが、これを殺すテストが存在しなかった)。"""
    auth = tmp_path / "creds" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"token":"chatgpt-subscription-secret"}')
    auth.chmod(0o600)
    captured: dict = {}
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        cfg_dir = Path(kw["cwd"]) / "cfg"
        captured["cfg_has_auth"] = (cfg_dir / "auth.json").is_file()
        return orig_popen([sys.executable, "-c", _READY_CHILD_SCRIPT], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(
        root=root,
        settings=_worker_settings(codex_backend=True, codex_provider="llama_swap",
                                  codex_auth_file=str(auth)),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve")
    runner.run(_mission())
    assert captured["cfg_has_auth"] is False, (
        "codex+llama_swap の scratch CODEX_HOME に auth.json をコピーしている "
        "— ChatGPT サブスクの資格情報がローカル LLM 相手の mission に漏れる")


def test_worker_runner_copies_auth_json_for_codex_chatgpt(monkeypatch, tmp_path):
    """上記の対: `codex + provider=chatgpt` では `auth.json` を
    コピーする (退行防止の対テスト)。"""
    auth = tmp_path / "creds" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"token":"chatgpt-subscription-secret"}')
    auth.chmod(0o600)
    captured: dict = {}
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        cfg_dir = Path(kw["cwd"]) / "cfg"
        captured["cfg_has_auth"] = (cfg_dir / "auth.json").is_file()
        return orig_popen([sys.executable, "-c", _READY_CHILD_SCRIPT], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(
        root=root,
        settings=_worker_settings(codex_backend=True, codex_provider="chatgpt",
                                  codex_auth_file=str(auth)),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve")
    runner.run(_mission())
    assert captured["cfg_has_auth"] is True


def test_worker_runner_rejects_credentials_file_that_is_a_symlink(monkeypatch, tmp_path):
    """認証原本の事前検査: 通常ファイル (`O_NOFOLLOW`) を要求する。"""
    # conftest の guard を通すため Popen をモック (実際には呼ばれない)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

    real = tmp_path / "real-creds.json"
    real.write_text('{"token":"x"}')
    real.chmod(0o600)
    link = tmp_path / "creds-link.json"
    link.symlink_to(real)
    root = _root(tmp_path)
    runner = WorkerRunner(
        root=root,
        settings=_worker_settings(claude_backend=True, credentials_file=str(link)),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve")
    result = runner.run(_mission())
    assert result.status == "failed"


def test_worker_runner_rejects_credentials_file_readable_by_group(monkeypatch, tmp_path):
    """認証原本の事前検査: group/other に権限が無いこと (mode 0600 系)。"""
    # conftest の guard を通すため Popen をモック (実際には呼ばれない)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

    creds = tmp_path / "creds.json"
    creds.write_text('{"token":"x"}')
    creds.chmod(0o644)
    root = _root(tmp_path)
    runner = WorkerRunner(
        root=root,
        settings=_worker_settings(claude_backend=True, credentials_file=str(creds)),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve")
    result = runner.run(_mission())
    assert result.status == "failed"


def test_worker_runner_rejects_oversized_credentials_file(monkeypatch, tmp_path):
    """認証原本の事前検査: サイズ ≤ 64 KiB。"""
    # conftest の guard を通すため Popen をモック (実際には呼ばれない)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

    creds = tmp_path / "creds.json"
    creds.write_bytes(b"x" * (65 * 1024))
    creds.chmod(0o600)
    root = _root(tmp_path)
    runner = WorkerRunner(
        root=root,
        settings=_worker_settings(claude_backend=True, credentials_file=str(creds)),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve")
    result = runner.run(_mission())
    assert result.status == "failed"


def test_trade_worker_no_longer_receives_data_provider_keys_via_env(monkeypatch):
    """R10-①: trade 資格情報を env から handshake へ移す。`_mission_worker_env`
    はどの profile にも資格情報を渡さない (env pin)。"""
    from agentic_fx.runners.worker_runner import _mission_worker_env

    monkeypatch.setenv("TWELVEDATA_API_KEY", "secret-td")
    monkeypatch.setenv("MT5_BRIDGE_API_KEY", "secret-mt5")
    env = _mission_worker_env("trade")
    assert "TWELVEDATA_API_KEY" not in env
    assert "MT5_BRIDGE_API_KEY" not in env


def test_trade_worker_receives_data_provider_keys_via_handshake(monkeypatch, tmp_path):
    """R10-①: trade worker は handshake フレームの `credentials` フィールド
    (stdin) で資格情報を受け取る。"""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "secret-td")
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        return orig_popen([sys.executable, "-c",
                           "import sys, json\n"
                           "line = sys.stdin.readline()\n"
                           "open('%s', 'w').write(line)\n"
                           "print(json.dumps({'type':'ready','seq':1,'ok':True}))\n"
                           % str(tmp_path / 'handshake.json')], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="trade")
    runner.run(_mission())
    handshake = json.loads((tmp_path / "handshake.json").read_text())
    assert handshake["credentials"]["TWELVEDATA_API_KEY"] == "secret-td"


# precheck 2026-08-22 pass2: RB2
def test_worker_runner_run_context_adds_three_handshake_keys(monkeypatch, tmp_path):
    """レビュー1周目 C3: `run_context=` (`ImproveRunContext` 相当。ここでは
    duck-typing で足りる最小オブジェクトを使う) が非 None のとき、
    `mission_id`/`staging_dir`/`source_snapshot_dir` の 3 キーが
    handshake フレームへ載る。(差分再検証 RB2) `mission_id` は
    `ImproveRunContext` 上は int だが、handshake JSON へ載せる際に
    `str()` される (子側は str 前提、`Path.name` との比較のため) — この
    assert が `str(42)` を pin する。"""
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        return orig_popen([sys.executable, "-c",
                           "import sys, json\n"
                           "line = sys.stdin.readline()\n"
                           "open('%s', 'w').write(line)\n"
                           "print(json.dumps({'type':'ready','seq':1,'ok':True}))\n"
                           % str(tmp_path / 'handshake.json')], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)

    class _FakeRunContext:
        mission_id = 42
        staging_dir = tmp_path / "staging"
        source_snapshot_dir = tmp_path / "source"

    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="improve",
                          run_context=_FakeRunContext())
    runner.run(_mission())
    handshake = json.loads((tmp_path / "handshake.json").read_text())
    assert handshake["mission_id"] == "42"  # precheck pass2 RB2: str() 化
    assert handshake["staging_dir"] == str(tmp_path / "staging")
    assert handshake["source_snapshot_dir"] == str(tmp_path / "source")


def test_worker_runner_run_context_none_omits_three_handshake_keys(
        monkeypatch, tmp_path):
    """`run_context=None` (既定、trade profile 等) のとき、3 キーは
    handshake フレームに含まれない (未知キーの汚染防止)。"""
    orig_popen = subprocess.Popen

    def spy(*a, **kw):
        return orig_popen([sys.executable, "-c",
                           "import sys, json\n"
                           "line = sys.stdin.readline()\n"
                           "open('%s', 'w').write(line)\n"
                           "print(json.dumps({'type':'ready','seq':1,'ok':True}))\n"
                           % str(tmp_path / 'handshake.json')], **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="trade")
    runner.run(_mission())
    handshake = json.loads((tmp_path / "handshake.json").read_text())
    assert "mission_id" not in handshake
    assert "staging_dir" not in handshake
    assert "source_snapshot_dir" not in handshake


# Step 36a-36d: cli_started pgid recovery tests
_FAKE_CHILD_WITH_CLI_STARTED = (
    "import json, subprocess, sys, time\n"
    "marker_path = sys.argv[1]\n"
    "sys.stdin.readline()\n"  # handshake を読み捨てる
    "cli = subprocess.Popen([sys.executable, '-c',"
    " 'import time; time.sleep(600)'], start_new_session=True)\n"
    "open(marker_path, 'w').write(str(cli.pid))\n"
    "sys.stdout.write(json.dumps({'type': 'cli_started', 'seq': 1,"
    " 'pgid': cli.pid}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdout.write(json.dumps({'type': 'ready', 'seq': 2,"
    " 'ok': True}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(600)\n"
)


def test_worker_runner_reaps_cli_pgid_after_worker_is_sigkilled(monkeypatch, tmp_path):
    """§7.1-2 の blocking 受入条件 (a): fake CLI (別 pgid) が sleep している
    状態で mission_worker (子) を SIGKILL しても、親 (WorkerRunner) は
    `cli_started` で得た CLI の pgid を回収し、生存プロセスを 0 にする。"""
    root = _root(tmp_path)
    marker = tmp_path / "cli_pid"
    script = tmp_path / "fake_child.py"
    script.write_text(_FAKE_CHILD_WITH_CLI_STARTED)

    real_popen = subprocess.Popen
    spawned: dict = {}

    def fake_popen(cmd, **kwargs):
        p = real_popen([sys.executable, str(script), str(marker)], **kwargs)
        spawned["proc"] = p
        return p

    monkeypatch.setattr("agentic_fx.runners.worker_runner.subprocess.Popen",
                        fake_popen)
    settings = _tiny_worker_settings(worker_startup_timeout_sec=5.0,
                                     worker_grace_sec=5.0)
    runner = WorkerRunner(root=root, settings=settings, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="improve")

    result_box: dict = {}
    thread = threading.Thread(
        target=lambda: result_box.update(result=runner.run(_mission())))
    thread.start()
    deadline = time.monotonic() + 5.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "fake CLI が cli_started を送る前にタイムアウトした"
    cli_pid = int(marker.read_text())
    os.kill(spawned["proc"].pid, signal.SIGKILL)  # mission_worker 相当を SIGKILL
    thread.join(timeout=15.0)
    assert not thread.is_alive(), "runner.run() が終わらない"

    deadline = time.monotonic() + 3.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.killpg(cli_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert not alive, "CLI の pgid が回収されず生存している"


# 指揮者検収 (B-4, プラン10 Task1) — 台帳 36d M3 の再判定。
# `_FAKE_CHILD_WITH_CLI_STARTED` の CLI は SIGTERM を無視しないため
# `_terminate_cli_pgid` の SIGKILL 昇格は原理的に到達しない、という台帳の
# 「観測不能」判定は誤りだった。SIGTERM を SIG_IGN する CLI
# (`tests/runners/test_launcher.py` の `_IGNORE_SIGTERM_AND_TOUCH` と同じ手)
# に差し替えると escalation は観測できる。
_FAKE_CHILD_WITH_SIGTERM_IGNORING_CLI = (
    "import json, subprocess, sys, time\n"
    "marker_path = sys.argv[1]\n"
    "sys.stdin.readline()\n"  # handshake を読み捨てる
    "cli = subprocess.Popen([sys.executable, '-c',"
    " 'import signal, sys, time;"
    " signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    " open(sys.argv[1], \"w\").write(\"ready\"); time.sleep(600)',"
    " sys.argv[2]], start_new_session=True)\n"
    "open(marker_path, 'w').write(str(cli.pid))\n"
    "sys.stdout.write(json.dumps({'type': 'cli_started', 'seq': 1,"
    " 'pgid': cli.pid}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdout.write(json.dumps({'type': 'ready', 'seq': 2,"
    " 'ok': True}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(600)\n"
)


def test_worker_runner_terminate_cli_pgid_escalates_to_sigkill_when_cli_ignores_sigterm(
        monkeypatch, tmp_path):
    """§7.1-2 の blocking 受入条件 (a) の変種: `cli_started` で得た CLI が
    SIGTERM を無視しても、`_terminate_cli_pgid` は grace 経過後に SIGKILL
    へ昇格して回収する (台帳 `tmp/mutation-ledger-task1.md` 36d M3 の
    再判定 — SIGKILL 昇格を `return` に変えると本テストは完走せず、
    孤児プロセスが残留する)。"""
    root = _root(tmp_path)
    marker = tmp_path / "cli_pid"
    ready_marker = tmp_path / "cli_ready"
    script = tmp_path / "fake_child.py"
    script.write_text(_FAKE_CHILD_WITH_SIGTERM_IGNORING_CLI)

    real_popen = subprocess.Popen
    spawned: dict = {}

    def fake_popen(cmd, **kwargs):
        p = real_popen(
            [sys.executable, str(script), str(marker), str(ready_marker)],
            **kwargs)
        spawned["proc"] = p
        return p

    monkeypatch.setattr("agentic_fx.runners.worker_runner.subprocess.Popen",
                        fake_popen)
    settings = _tiny_worker_settings(worker_startup_timeout_sec=5.0,
                                     worker_grace_sec=5.0)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(
            update={"cli_terminate_grace_sec": 0.5})})
    runner = WorkerRunner(root=root, settings=settings, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="improve")

    result_box: dict = {}
    # daemon=True: 変異注入時 (SIGKILL 昇格が消えた版) は CLI が生存し続け、
    # `proc.stdout` を読み中の reader スレッドと `finally` 節の
    # `proc.stdout.close()` が同じ io ロックを奪い合って恒久的にブロック
    # し得る (実測)。thread を daemon にしないとプロセス終了そのものが
    # 巻き添えでブロックする — 変異の観測 (test failure) 自体はこの前に
    # `thread.join(timeout=...)` のタイムアウトで確定するため daemon 化
    # しても red/green の判定には影響しない。
    thread = threading.Thread(
        target=lambda: result_box.update(result=runner.run(_mission())),
        daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "fake CLI が cli_started を送る前にタイムアウトした"
    cli_pid = int(marker.read_text())

    # cli_pid が判明した以降は assert 失敗時にも必ず孤児回収まで到達させる
    # (途中の assert で早期 return すると SIGTERM 無視プロセスが残留する
    # ことを実測で確認したため try/finally で括る)。
    try:
        # SIGTERM ハンドラの設定完了 ("ready" marker) を待たないと race する。
        deadline = time.monotonic() + 5.0
        while not ready_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_marker.exists(), "SIGTERM 無視 CLI が準備を終える前にタイムアウトした"

        os.kill(spawned["proc"].pid, signal.SIGKILL)  # mission_worker 相当を SIGKILL
        thread.join(timeout=15.0)
        assert not thread.is_alive(), "runner.run() が終わらない"

        deadline = time.monotonic() + 3.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.killpg(cli_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive, "SIGTERM を無視する CLI の pgid が SIGKILL へ昇格せず生存している"
    finally:
        # テストが落ちても SIGTERM 無視プロセスを孤児として残さない。
        try:
            os.killpg(cli_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_worker_runner_cli_started_never_sent_leaves_finally_a_no_op(
        tmp_path, monkeypatch):
    """§7.1-2 の blocking 受入条件 (b): `cli_started` が一度も来なければ、
    finally の CLI pgid 回収は no-op (架空の pgid へ killpg しない)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()
    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(wr_mod.os, "killpg",
                        lambda pid, sig: killpg_calls.append((pid, sig)))

    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    # `_ensure_dead` は fake_proc.pid (= os.getpid()) への killpg のみ —
    # cli_started 由来の追加 killpg 呼び出しは無い (架空 pgid への発砲防止)
    assert all(pid == fake_proc.pid for pid, _sig in killpg_calls)


# ---------------------------------------------------------------------------
# 裁定 R-D1 (プラン10 Task 9 再工事): `WorkerRunner.run()` は子の `ready`
# フレーム受信直後に `on_ready(frame)` を呼び、例外なく戻ったらそのまま
# 実行を継続する。新規プロトコルフレーム (`go` 等) は追加しない — 既存の
# 一発 handshake 方式のまま、`on_ready` を「ready 直後のフック」として
# 使うだけ (プラン 9.3 節「実装時改訂 (2026-08-22 深夜裁定 R-D1)」参照)。
# `on_ready` が例外を投げたら子を kill して `MissionResult('failed', ...)`
# を返す (pre-ready 失敗)。
# ---------------------------------------------------------------------------


def _fake_improve_proc(r, w, w2, r2):
    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    return FakeProc()


def test_on_ready_called_before_result_and_mission_completes_normally(
        tmp_path, monkeypatch):
    """`on_ready` が例外なく戻ったら、`run()` は新規フレームを挟まず
    そのまま `result` まで読み進め、通常どおり完走する (裁定 R-D1 —
    新規の `go` フレームは追加しない)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()
    events: list[str] = []

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake (seq=1)
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        # R-D1: 新規フレームを待たず、ready 送出直後に直接 result を返す
        # (現物の実行継続ロジックのまま)。
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)
    fake_proc = _fake_improve_proc(r, w, w2, r2)

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    def _on_ready(frame):
        events.append(f"on_ready:{frame['ok']}")

    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="improve",
                          on_ready=_on_ready)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert events == ["on_ready:True"], (
        "on_ready が呼ばれていない、または期待どおりの frame を受け取って"
        "いない")


def test_on_ready_exception_kills_child_and_returns_failed(tmp_path, monkeypatch):
    """`on_ready` が例外を投げたら、子を SIGTERM→SIGKILL で止め、
    `MissionResult(status='failed', reason=...)` を返す (裁定 R-D1 —
    pre-ready 失敗として `ImproveSupervisor` が扱えるようにする)。新規
    フレームは無いので、子は「親が次に何も書いてこない (stdin が閉じられ
    EOF になる)」ことでこの失敗を観測する。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()
    child_saw_eof = threading.Event()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        line = child_in.readline()  # 次のフレームを待つ — 来ないはず
        if line == b"":
            child_saw_eof.set()
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)
    fake_proc = _fake_improve_proc(r, w, w2, r2)

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(wr_mod.os, "killpg",
                        lambda pid, sig: killpg_calls.append((pid, sig)))

    def _boom(frame):
        raise RuntimeError("on_ready boom")

    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=_tiny_worker_settings(),
                          clock=FixedClock(NOW), rag=_rag(tmp_path),
                          worker_profile="improve", on_ready=_boom)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"
    assert result.reason is not None and "on_ready failed" in result.reason
    assert child_saw_eof.is_set(), (
        "on_ready 失敗後も親が何か書き込んでいる (kill されていない疑い)")
    assert any(sig == signal.SIGTERM for _pid, sig in killpg_calls), (
        "on_ready 失敗時に子が SIGTERM されていない")


def test_on_ready_called_for_trade_profile_too(tmp_path, monkeypatch):
    """`on_ready` は worker_profile に関わらず ready 直後に呼ばれる —
    trade profile 用の分岐は無い (R-D1 は新規フレームを追加しないため、
    improve/trade で `run()` の実行継続ロジックに差は生まれない)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)
    fake_proc = _fake_improve_proc(r, w, w2, r2)

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    on_ready_calls: list[dict] = []
    root = _root(tmp_path)
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=FixedClock(NOW),
                          rag=_rag(tmp_path), worker_profile="trade",
                          on_ready=on_ready_calls.append)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert len(on_ready_calls) == 1


def test_real_improve_worker_ready_then_on_ready_then_result_ordering(tmp_path):
    """R-D1 pin (c): 実プロセスの `mission_worker.py` (improve profile) を
    本物の `WorkerRunner` で起動し、`ready` → `on_ready` → `result` の
    順序 (新規フレームなし) を実測する。llama-swap への接続先は到達不能
    アドレスへ差し替えているため `LocalRunner.run()` は速やかに失敗する
    が、それでも `on_ready` が呼ばれ、mission がタイムアウトいっぱいまで
    待たされずに `failed` で返ることを確認する — 子がフレームを待たず
    ready 直後に実行を継続していることの間接証拠。"""
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")
    from tests.conftest import _LLAMA_SWAP_UNREACHABLE_URL

    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "llama_swap": SETTINGS.llama_swap.model_copy(
            update={"base_url": _LLAMA_SWAP_UNREACHABLE_URL, "timeout_sec": 1}),
        "worker": SETTINGS.worker.model_copy(
            update={"worker_startup_timeout_sec": 15.0,
                    "worker_grace_sec": 5.0,
                    "worker_terminate_grace_sec": 2.0})})

    staging = tmp_path / "staging" / "rd1-real-probe"
    staging.mkdir(parents=True, mode=0o700)
    source_snapshot = tmp_path / "source"
    source_snapshot.mkdir(mode=0o500)

    class _RunContext:
        mission_id = "rd1-real-probe"
        staging_dir = staging
        source_snapshot_dir = source_snapshot

    events: list[str] = []

    def _on_ready(frame):
        events.append("on_ready")
        assert frame.get("ok") is True, frame

    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path), worker_profile="improve",
                          run_context=_RunContext(), on_ready=_on_ready)
    mission = Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=20.0)
    started = time.monotonic()
    result = runner.run(mission)
    elapsed = time.monotonic() - started

    assert events == ["on_ready"], (
        "on_ready が呼ばれていない、または複数回呼ばれた")
    assert result.status == "failed", result
    # 子が ready 送出直後に実行を継続し (新規フレームを待たず)、
    # LocalRunner.run() まで進んで (到達不能な llama-swap への) 接続失敗
    # で速やかに failed を返したことの間接証拠 — 何かを待ち続けていれば
    # mission.timeout_sec (20s) いっぱいまで待たされる。
    assert elapsed < mission.timeout_sec, (
        f"mission timeout いっぱいまで待たされた (子が実行を継続していない"
        f"疑い): elapsed={elapsed:.2f}s")


def test_real_improve_worker_on_ready_exception_leaves_no_surviving_child(
        tmp_path, monkeypatch):
    """R-D1 pin (b) の「子が残らない」を実プロセスで実測する。
    `test_on_ready_exception_kills_child_and_returns_failed` は
    `os.killpg` を monkeypatch した fake 子 (プロセスとして実在しない)
    で `killpg` の呼び出しだけを確認するため、実際に子が死ぬところまでは
    見ていない — ここでは本物の `mission_worker.py` 子プロセスを起動し、
    `on_ready` が例外を投げた後、実際の pid が `poll()` で非 None (終了
    済み) になることを確認する。子は on_ready 例外と競合して
    `LocalRunner.run()` (llama-swap への実リクエスト) へ進みうる — 到達
    不能アドレスへ差し替え、実サーバへリクエストを飛ばさない
    (`test_real_improve_worker_ready_then_on_ready_then_result_ordering`
    と同じ規律)。"""
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")
    from tests.conftest import _LLAMA_SWAP_UNREACHABLE_URL

    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "llama_swap": SETTINGS.llama_swap.model_copy(
            update={"base_url": _LLAMA_SWAP_UNREACHABLE_URL, "timeout_sec": 1}),
        "worker": SETTINGS.worker.model_copy(
            update={"worker_startup_timeout_sec": 15.0,
                    "worker_grace_sec": 5.0,
                    "worker_terminate_grace_sec": 2.0})})

    staging = tmp_path / "staging" / "rd1-real-kill-probe"
    staging.mkdir(parents=True, mode=0o700)
    source_snapshot = tmp_path / "source"
    source_snapshot.mkdir(mode=0o500)

    class _RunContext:
        mission_id = "rd1-real-kill-probe"
        staging_dir = staging
        source_snapshot_dir = source_snapshot

    captured: dict[str, object] = {}
    import agentic_fx.runners.worker_runner as wr_mod
    real_popen = wr_mod.subprocess.Popen

    def _capture(*a, **k):
        p = real_popen(*a, **k)
        captured["proc"] = p
        return p

    monkeypatch.setattr(wr_mod.subprocess, "Popen", _capture)

    def _boom(frame):
        raise RuntimeError("on_ready boom (real process probe)")

    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path), worker_profile="improve",
                          run_context=_RunContext(), on_ready=_boom)
    mission = Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=20.0)
    result = runner.run(mission)

    assert result.status == "failed"
    assert result.reason is not None and "on_ready failed" in result.reason
    proc = captured["proc"]
    assert proc.poll() is not None, (
        "on_ready 失敗後も子プロセスが生きている (kill されていない疑い)")

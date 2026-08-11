"""mission_worker.py の単体テスト (インプロセス — 実 subprocess は spawn しない)。"""
from __future__ import annotations

import ctypes
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx import mission_worker
from agentic_fx.config import load_settings
from agentic_fx.store import signals
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import signal_tools


def test_set_pdeathsig_calls_prctl_with_expected_args(monkeypatch):
    calls: list[tuple] = []

    class FakeLibc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(mission_worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    mission_worker._set_pdeathsig(15)  # SIGTERM
    assert calls == [(1, 15, 0, 0, 0)]  # PR_SET_PDEATHSIG=1


def test_set_pdeathsig_raises_oserror_on_failure(monkeypatch):
    class FakeLibc:
        def prctl(self, *args):
            return -1

    monkeypatch.setattr(mission_worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(mission_worker.ctypes, "get_errno", lambda: 1)
    with pytest.raises(OSError):
        mission_worker._set_pdeathsig(15)


def test_rag_rpc_proxy_search_news_round_trip():
    """tool_rpc → tool_rpc_result の同期往復。

    CR-3 対応の回帰ピン: `out_seq` は `main()` が `ready`/`event`/`result`
    の送出に使うのと**同一インスタンス**を渡す (このテストでは 1 度も
    他フレームを送出していないので `out_seq` の初期値は 1 のまま —
    `tool_rpc` の seq は 1 になる)。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    outbound = io.BytesIO()
    out_seq = SeqTracker()
    in_seq = SeqTracker()

    def write_fn(frame):
        outbound.write((json.dumps(frame) + "\n").encode())

    # fake 親: search_news の RPC 要求に対して固定結果を返す応答を用意する
    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": True,
                "result": [{"title": "t", "body": "b", "source_name": "s"}]}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, out_seq, in_seq)
    result = proxy.search_news("usdjpy", n=5)
    assert result == [{"title": "t", "body": "b", "source_name": "s"}]

    outbound.seek(0)
    sent = json.loads(outbound.getvalue().splitlines()[0])
    assert sent["type"] == "tool_rpc"
    assert sent["seq"] == 1
    assert sent["name"] == "search_news"
    assert sent["args"] == {"query": "usdjpy", "n": 5}


def test_rag_rpc_proxy_shares_out_seq_with_other_child_to_parent_frames():
    """CR-3 の直接回帰ピン: `main()` が `ready` (seq=1) を送出済みの状態を
    模して `out_seq` を 1 個進めてから `_RagRpcProxy` に渡すと、
    `tool_rpc` の seq は 2 になる (親の単一 `in_seq` — 全フレーム種別
    共通 — が期待する次の値と一致する)。独立した `SeqTracker` を渡すと
    ここが 1 に戻ってしまい、親側で `ProtocolError` になる。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    outbound = io.BytesIO()
    out_seq = SeqTracker()
    out_seq._expected = 2  # ready (seq=1) を送出済みの状態を模す
    in_seq = SeqTracker()

    def write_fn(frame):
        outbound.write((json.dumps(frame) + "\n").encode())

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": True,
                "result": []}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, out_seq, in_seq)
    proxy.search_news("usdjpy")

    outbound.seek(0)
    sent = json.loads(outbound.getvalue().splitlines()[0])
    assert sent["seq"] == 2


def test_rag_rpc_proxy_propagates_error():
    from agentic_fx.core.mission_protocol import SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": False,
                "error": "rag unavailable"}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker(), SeqTracker())
    with pytest.raises(RuntimeError, match="rag unavailable"):
        proxy.search_news("q")


def test_rag_rpc_proxy_rejects_wrong_frame_type(): 
    """I2 対応: 親→子方向 (tool_rpc_result) の type 検証。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "event", "seq": 1, "rpc_id": "1", "ok": True, "result": []}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker(), SeqTracker())
    with pytest.raises(ProtocolError, match="tool_rpc_result"):
        proxy.search_news("q")


def test_rag_rpc_proxy_rejects_seq_gap():
    """I2 対応: 親→子方向の seq 検証 (欠番/重複/逆行を一律 ProtocolError)。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 5, "rpc_id": "1", "ok": True,
                "result": []}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker(), SeqTracker())
    with pytest.raises(ProtocolError, match="seq"):
        proxy.search_news("q")


def test_on_message_exits_process_on_serialize_failure(monkeypatch):
    """I6 対応: event フレームを送れなかったら `os._exit(1)` で即座に
    プロセスを終了する (`LocalRunner._sink` の fail-soft に頼って継続
    しない — 親は transcript の一部を永久に受け取れなくなる)。

    レビュー 2 周目 (codex) 以降、**transport 失敗は
    `_write_frame_or_die` がプロセスごと落とす**ため、この節へ実際に
    到達するのは serialize 失敗 (message が JSON にならない) のとき。
    `_make_on_message` 自身の guard を単独で pin するため、ここでは
    serialize 失敗を注入する (transport 失敗を注入すると
    `_write_frame_or_die` 側で exit してしまい、この try/except を
    削除しても green のままになる)。"""
    import io as _io

    from agentic_fx.core.mission_protocol import SeqTracker

    exit_calls: list[int] = []
    monkeypatch.setattr(mission_worker.os, "_exit", exit_calls.append)

    out_seq = SeqTracker()
    stream = _io.BytesIO()
    on_message = mission_worker._make_on_message(stream, out_seq)
    on_message({"role": "assistant", "content": object()})  # JSON 化不可

    assert exit_calls == [1]
    assert stream.getvalue() == b""       # wire には何も出ていない
    assert out_seq._expected == 1          # seq も消費していない


def test_write_frame_or_die_exits_on_transport_failure(monkeypatch):
    """レビュー 2 周目 (codex): `write`/`flush` の例外は「wire に 1 バイトも
    出ていない」ことを保証しない (部分書込み / flush 後の失敗)。同じ seq で
    再送すると壊れた行か重複フレームを作るため、**再送せず即終了**する。"""
    exit_calls: list[int] = []
    monkeypatch.setattr(mission_worker.os, "_exit", exit_calls.append)

    class BrokenStream:
        def write(self, data):
            raise BrokenPipeError("broken pipe")

        def flush(self):
            pass

    mission_worker._write_frame_or_die(BrokenStream(), {"type": "ready", "seq": 1})
    assert exit_calls == [1]


def test_write_frame_or_die_exits_when_flush_fails(monkeypatch):
    """`write` が成功しても `flush` が失敗すれば配信は確定できない
    (同じ防御を 2 方向から壊す — 変異リストは下限であって天井ではない)。"""
    exit_calls: list[int] = []
    monkeypatch.setattr(mission_worker.os, "_exit", exit_calls.append)

    class FlushBrokenStream:
        def __init__(self):
            self.written = b""

        def write(self, data):
            self.written += data

        def flush(self):
            raise BrokenPipeError("flush failed")

    mission_worker._write_frame_or_die(
        FlushBrokenStream(), {"type": "ready", "seq": 1})
    assert exit_calls == [1]


def test_rag_rpc_proxy_does_not_advance_out_seq_when_send_fails():
    """sonnet 副査 2 周目 (単独検出): `_call` の「送出成功後に採番を進める」
    性質が未 pin だった (pre-increment に戻しても全 1499 件が green)。
    `_send_frame` と同じ append-site の穴 (Task 6 で同型の指摘)。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    out_seq = SeqTracker()

    def failing_write(frame):
        raise BrokenPipeError("send failed")

    proxy = mission_worker._RagRpcProxy(
        failing_write, lambda: None, out_seq, SeqTracker())
    with pytest.raises(BrokenPipeError):
        proxy.search_news("q")

    assert out_seq._expected == 1  # 消費していない


def test_main_rejects_handshake_with_wrong_type(monkeypatch, tmp_path):
    """I2 対応: 子は handshake フレームの type/seq を検証してから bootstrap
    に進む — 不一致なら ready を送らず即終了する (fail closed)。"""
    import io as _io

    frame = json.dumps({"type": "event", "seq": 1}).encode() + b"\n"
    monkeypatch.setattr(mission_worker.sys, "stdin",
                        type("S", (), {"buffer": _io.BytesIO(frame)})())
    captured = _io.BytesIO()
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout",
                        lambda: captured)

    mission_worker.main()

    captured.seek(0)
    lines = captured.getvalue().splitlines()
    assert len(lines) == 1
    sent = json.loads(lines[0])
    assert sent["type"] == "ready"
    assert sent["ok"] is False
    # 2026-08-08 指揮者の変異テストで追加: `ok is False` だけでは type
    # 検証を削除しても green のまま (後続の
    # `handshake["expected_parent_pid"]` が KeyError を投げ、外側 except
    # が同じ ready:false を返すため)。「type 違反として検出したこと」
    # までを pin する。
    assert "handshake" in sent["error"]


def test_main_rejects_handshake_with_wrong_seq(monkeypatch, tmp_path):
    """I2 対応 (2026-08-08 指揮者の着手前照合で追加): `main()` の
    `in_seq.check(handshake.get("seq"))` を単独で pin する。

    `test_main_rejects_handshake_with_wrong_type` は type 検証が先に
    `ProtocolError` を送出するため **seq 検証行に到達しない** — その行を
    削除しても green のままになる (Task 2 の `--noconftest` と同じ
    「防御はあるがテストが無い」型の穴)。本テストは `type` を正しい
    `"handshake"` にしたうえで `seq` だけを不正にし、seq 検証行だけを
    red で守る。"""
    import io as _io

    frame = json.dumps({"type": "handshake", "seq": 7}).encode() + b"\n"
    monkeypatch.setattr(mission_worker.sys, "stdin",
                        type("S", (), {"buffer": _io.BytesIO(frame)})())
    captured = _io.BytesIO()
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout",
                        lambda: captured)

    mission_worker.main()

    captured.seek(0)
    lines = captured.getvalue().splitlines()
    assert len(lines) == 1
    sent = json.loads(lines[0])
    assert sent["type"] == "ready"
    assert sent["ok"] is False
    assert "seq" in sent["error"]


def test_rag_rpc_proxy_in_seq_continues_after_handshake():
    """親→子方向の seq 連続性 pin (2026-08-08 指揮者の着手前照合で追加)。

    `in_seq` は `main()` が `handshake` (seq=1) の検証に使うのと**同一
    インスタンス**であるため、本番では**最初の `tool_rpc_result` は
    seq=2** でなければならない。他の RPC テストはいずれも新品の `in_seq`
    に `seq: 1` を食わせており、この連続性を pin していない (どちらの
    実装でも green になる)。Task 10 の親側実装者がそれらをワイヤ仕様と
    読むと最初の `tool_rpc_result` を seq=1 で送り、**RAG 検索を使う
    Mission が初回呼出しで必ず `ProtocolError` になる** — CR-3 の
    親→子方向の鏡像。本テストがその契約を固定する。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    def write_fn(frame):
        pass

    in_seq = SeqTracker()
    in_seq.check(1)  # main() が handshake (seq=1) を検証済みの状態

    proxy_ok = mission_worker._RagRpcProxy(
        write_fn,
        lambda: {"type": "tool_rpc_result", "seq": 2, "rpc_id": "1",
                 "ok": True, "result": []},
        SeqTracker(), in_seq)
    assert proxy_ok.search_news("q") == []

    in_seq2 = SeqTracker()
    in_seq2.check(1)
    proxy_ng = mission_worker._RagRpcProxy(
        write_fn,
        lambda: {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1",
                 "ok": True, "result": []},
        SeqTracker(), in_seq2)
    with pytest.raises(ProtocolError, match="seq"):
        proxy_ng.search_news("q")


_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_build_clock_default_rejects_stale_signal_as_wall_clock_advances(
        monkeypatch, tmp_path):
    """(I4/R2-CX-01) `_build_clock()` の既定 (`SystemClock()`) は呼ぶたび
    に壁時計を再評価する — Mission 実行中に実時間が進むと、`get_signals`
    の鮮度窓の基準時刻も一緒に進み、窓の外に出た signal は除外され続ける
    (fail closed)。`SystemClock.now` を monkeypatch して「1 回目の呼び出し
    は handshake 直後・2 回目は 45 分後」の壁時計を模擬し、同一 signal が
    1 回目は含まれ 2 回目は除外されることを検証する。

    `_build_clock()` の戻り値を `FixedClock(...)` (handshake 時点で 1 回
    だけ `now()` を取得して固定) に変異させると、2 回目の呼び出しでも
    基準時刻が進まず signal が除外されなくなる (fail-open) — 本テストは
    その場合に red になる (Task 20 の E2E は資金保護用の FixedClock しか
    使わず worker 内時刻を進めるシナリオを持たないため、この退行を拾える
    のは本テストのみ)。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config"
        / "settings.yaml.example")

    # signal は handshake 時点 (_BASE) の 30 分前に観測された市場イベント
    # (bar_ts 基準)。since_hours=1 (60分) の窓には handshake 直後は入るが、
    # 壁時計が 45 分進んだ 2 回目には 75 分前になり窓 (60分) の外に出る。
    signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(_BASE - timedelta(minutes=30)).isoformat(),
        kind="signal", payload={"x": 1}, now=_BASE)

    wall_clock_ticks = iter([_BASE, _BASE + timedelta(minutes=45)])
    monkeypatch.setattr(
        "agentic_fx.core.contracts.SystemClock.now",
        lambda self: next(wall_clock_ticks))

    # `_build_clock()` 自身を呼ぶ (monkeypatch しない) — I4 変異
    # (SystemClock() → FixedClock(...)) を検出する対象はこの呼び出し。
    clock = mission_worker._build_clock()
    get_signals = signal_tools.build(conn, settings, clock)[0].func

    out_immediately = get_signals(pair="USDJPY", since_hours=1)
    assert {r["content_hash"] for r in out_immediately} == {"h1"}

    out_45min_later = get_signals(pair="USDJPY", since_hours=1)
    assert out_45min_later == []


# ---------------------------------------------------------------------------
# main() の bootstrap 本体 (指揮者が Task 7 の変異テストで追加 — 2026-08-08)
#
# プラン Step 5 のテスト 12 本は `_RagRpcProxy`/`_make_on_message`/
# `_set_pdeathsig`/`_build_clock` の**単体**と、`main()` の**外側 except に
# 落ちる 2 経路**しかカバーしていない。その結果、`main()` の bootstrap 本体
# (ppid 照合・rlimit・profile 検証・backend fail-closed・readonly=True・
# out_seq/in_seq の共有配線・ready/result 送出) が全て無防備で、変異 16 件が
# 生存した。以下はその配線そのものを pin する (「単体が全部緑でも誰からも
# 呼ばれない・届かない経路は検出されない」)。
# ---------------------------------------------------------------------------


def _handshake_settings() -> dict:
    from agentic_fx.config import load_settings
    return load_settings(
        Path(__file__).resolve().parents[1] / "config"
        / "settings.yaml.example").model_dump(mode="json")


class _FakeLocalRunner:
    """`LocalRunner` の差し替え。`run()` で on_message を 1 回呼び、
    `MissionResult` を返す。"""

    instances: list["_FakeLocalRunner"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.raise_on_run = False
        _FakeLocalRunner.instances.append(self)

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        if self.raise_on_run:
            raise RuntimeError("runner exploded")
        self.kwargs["on_message"]({"role": "assistant", "content": "hi"})
        return MissionResult(status="completed", output={"action": "no_trade"})


def _drive_main(monkeypatch, tmp_path, *, handshake_overrides=None,
                settings_mutator=None, runner_raises=False,
                raw_stdin=None, out_stream=None, resource_limits_raise=False,
                runner_cls=None):
    """`main()` をインプロセスで駆動し、送出フレーム列と観測点を返す。

    `raw_stdin`: handshake の JSON 化を飛ばして生バイト列を stdin に流す
    (不正 JSON の検証用)。`out_stream`: `_protect_protocol_stdout` の戻り値を
    差し替える (送出失敗の注入用)。`resource_limits_raise`:
    `_set_resource_limits` を例外送出する fake にする (fail closed の検証用)。
    """
    import io as _io

    from agentic_fx.store.db import connect, init_db

    db_path = tmp_path / "t.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()

    settings_dict = _handshake_settings()
    if settings_mutator is not None:
        settings_mutator(settings_dict)

    handshake = {
        "type": "handshake", "seq": 1,
        "expected_parent_pid": os.getppid(),
        "settings": settings_dict,
        "worker_profile": "trade",
        "db_path": str(db_path),
        "plugins_dir": None,
        "mission": {"prompt": "p", "tools": ["get_quote"], "output_schema": {},
                    "max_turns": 3, "timeout_sec": 30.0},
    }
    handshake.update(handshake_overrides or {})

    stdin_bytes = (raw_stdin if raw_stdin is not None
                   else json.dumps(handshake).encode() + b"\n")
    monkeypatch.setattr(
        mission_worker.sys, "stdin",
        type("S", (), {"buffer": _io.BytesIO(stdin_bytes)})())
    captured = out_stream if out_stream is not None else _io.BytesIO()
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout",
                        lambda: captured)

    # 実プロセスの rlimit を壊さないため差し替える (呼び出し引数だけを見る)。
    rlimit_calls: list[dict] = []

    def fake_set_resource_limits(**kw):
        rlimit_calls.append(kw)
        if resource_limits_raise:
            raise OSError(1, "setrlimit refused by hard limit")

    monkeypatch.setattr(mission_worker, "_set_resource_limits",
                        fake_set_resource_limits)

    registry_calls: list[tuple] = []

    def fake_build_mission_registry(*args, **kwargs):
        registry_calls.append((args, kwargs))
        from agentic_fx.tools.registry import ToolRegistry
        return ToolRegistry()

    monkeypatch.setattr("agentic_fx.tools.mission_registry.build_mission_registry",
                        fake_build_mission_registry)
    _FakeLocalRunner.instances = []
    monkeypatch.setattr("agentic_fx.runners.local_runner.LocalRunner",
                        runner_cls if runner_cls is not None else _FakeLocalRunner)
    if runner_raises:
        orig_init = _FakeLocalRunner.__init__

        def init_raising(self, **kw):
            orig_init(self, **kw)
            self.raise_on_run = True
        monkeypatch.setattr(_FakeLocalRunner, "__init__", init_raising)

    mission_worker.main()

    raw = (bytes(captured.buf) if out_stream is not None
           else captured.getvalue())
    frames = [json.loads(l) for l in raw.splitlines()]
    return frames, rlimit_calls, registry_calls


def test_main_happy_path_emits_ready_event_result_in_one_seq_sequence(
        monkeypatch, tmp_path):
    """正常経路の配線 pin。子→親の全フレーム種別が**単一の out_seq**で
    1 起点連番になる (設計書 §4.3 codex M2-1、親側 WorkerRunner がそう
    検証する)。`ready`(1) → `event`(2) → `result`(3)。"""
    frames, _, _ = _drive_main(monkeypatch, tmp_path)

    assert [f["type"] for f in frames] == ["ready", "event", "result"]
    assert [f["seq"] for f in frames] == [1, 2, 3]
    assert frames[0]["ok"] is True
    assert frames[1]["message"] == {"role": "assistant", "content": "hi"}
    assert frames[2]["status"] == "completed"
    assert frames[2]["output"] == {"action": "no_trade"}


def test_main_shares_out_seq_and_in_seq_with_rag_rpc_proxy(monkeypatch, tmp_path):
    """CR-3 (out_seq) と I2 (in_seq) の**配線**の回帰ピン。

    既存の `test_rag_rpc_proxy_shares_out_seq_...` は `_RagRpcProxy` を直接
    構築するため**契約**しか pin できず、`main()` の call site を独立した
    `SeqTracker()` に差し替える変異が生存していた (プラン Step 11 の変異
    4/9 が期待どおりに red にならない — 実測)。ここでは `main()` が実際に
    proxy へ渡したインスタンスを観測する。"""
    _, _, registry_calls = _drive_main(monkeypatch, tmp_path)

    (args, _kwargs) = registry_calls[0]
    proxy = args[4]
    # out_seq: ready(1)/event(2)/result(3) を採番したので次は 4。独立
    # インスタンスを渡す変異では 1 のまま。
    assert proxy._out_seq._expected == 4
    # in_seq: handshake(seq=1) を検証済みなので次は 2 (= 親が送る最初の
    # tool_rpc_result の seq)。独立インスタンスを渡す変異では 1 のまま。
    assert proxy._in_seq._expected == 2


def test_main_builds_registry_readonly_and_applies_resource_limits(
        monkeypatch, tmp_path):
    """CR-4 (`readonly=True` — 子は RO 接続なので cache 書込をスキップ) と
    §12 申し送り② (rlimit を settings の確定値で適用する) の配線ピン。"""
    _, rlimit_calls, registry_calls = _drive_main(monkeypatch, tmp_path)

    assert rlimit_calls == [{"as_mb": 4096, "nofile": 128, "fsize_mb": 8}]
    (args, kwargs) = registry_calls[0]
    assert args[0] == "trade"
    assert kwargs["readonly"] is True
    assert kwargs["indicator_plugins"] == []


def test_main_reports_result_failed_when_runner_raises(monkeypatch, tmp_path):
    """`runner.run()` が例外を投げても **必ず `result` を送る** (親が EOF で
    はなく明示的な失敗として Mission を終端できる)。"""
    frames, _, _ = _drive_main(monkeypatch, tmp_path, runner_raises=True)

    assert [f["type"] for f in frames] == ["ready", "result"]
    assert frames[1]["seq"] == 2
    assert frames[1]["status"] == "failed"
    assert frames[1]["output"] is None
    assert "runner exploded" in frames[1]["error"]


def test_main_exits_without_ready_when_reparented(monkeypatch, tmp_path):
    """設計書 §4.8 codex I2-4: `_set_pdeathsig` 設定前に親が死んで再親付け
    されたレース。`expected_parent_pid` と実際の `os.getppid()` が食い違う
    場合は **ready を送らずに即終了**する (親の起動 timeout が検出する)。"""
    frames, rlimit_calls, registry_calls = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides={"expected_parent_pid": os.getppid() + 1})

    assert frames == []
    assert rlimit_calls == []
    assert registry_calls == []


def test_main_rejects_unsupported_worker_profile(monkeypatch, tmp_path):
    """本プランのスコープは `worker_profile="trade"` と Task 18 で実装した
    `worker_profile="improve"` のみ。それ以外（例: bogus）は ready を送る前に
    fail closed する。"""
    frames, _, registry_calls = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides={"worker_profile": "bogus"})

    assert len(frames) == 1
    assert frames[0]["type"] == "ready" and frames[0]["ok"] is False
    assert "bogus" in frames[0]["error"]
    assert registry_calls == []


def test_main_applies_landlock_bootstrap_before_running_improve_mission(
        monkeypatch, tmp_path):
    """**Task 18 の中心的防御の配線ピン** (プラン8, 設計書 §4.6)。

    improve 分岐は `_bootstrap_improve_profile()` を、依存 import・
    `LocalRunner` 構築・`ready` 送出のいずれよりも**先に必ず呼ぶ**。

    根拠 (指揮者の段0 変異スイープで実測): `main()` からこの呼び出しを
    削除しても `tests/test_improve_profile_isolation.py` は 5 件全て green
    のままだった。あちらは `_bootstrap_improve_profile` を probe から
    **直接**呼ぶので、「`main()` がそれを呼ぶか」を一切見ていない。
    削除されると **improve worker が Landlock 無しで走る** — 本 task が
    作った権限境界が丸ごと消える。

    実 Landlock は不可逆 (プロセス生涯にわたって有効) なので、ここでは
    呼び出しの有無と順序だけを観測する。実際の遮断の検証は
    `tests/test_improve_profile_isolation.py` の実 subprocess 帯が持つ。
    """
    calls: list[int] = []
    monkeypatch.setattr(
        mission_worker, "_bootstrap_improve_profile",
        lambda: calls.append(len(_FakeLocalRunner.instances)))

    frames, _, registry_calls = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides={"worker_profile": "improve",
                             "db_path": None, "plugins_dir": None})

    assert calls, ("main() の improve 分岐が _bootstrap_improve_profile を"
                   "呼んでいない — Landlock 無しで improve worker が走る")
    assert calls[0] == 0, ("_bootstrap_improve_profile が LocalRunner 構築"
                           "より後に呼ばれている (Landlock 適用前に依存を"
                           "読み込む順序になっている)")
    assert frames[0]["type"] == "ready" and frames[0]["ok"] is True
    # improve profile は trade の registry を組まない (DB 非参照の構造的成立)
    assert registry_calls == []


def test_main_fails_closed_when_runner_backend_is_claude(monkeypatch, tmp_path):
    """**Global Constraints の強制点**: Anthropic API (従量課金) は使用不可で
    `ClaudeRunner` は本プラン未実装。`runner.trade.backend == "claude"` を子が
    検出したら `RuntimeError` で fail closed する (Mission を実行しない)。"""
    def to_claude(d):
        d["runner"]["trade"]["backend"] = "claude"

    frames, _, registry_calls = _drive_main(
        monkeypatch, tmp_path, settings_mutator=to_claude)

    assert len(frames) == 1
    assert frames[0]["type"] == "ready" and frames[0]["ok"] is False
    assert "claude" in frames[0]["error"].lower()
    assert registry_calls == []


def test_set_resource_limits_sets_all_four_limits(monkeypatch):
    """`_set_resource_limits` が RLIMIT_AS/NOFILE/FSIZE/CORE の 4 本すべてを
    指定値ちょうどで設定する (実プロセスへ適用すると pytest 自体を壊すため
    `resource.setrlimit` を fake で観測する — `plugin/worker.py` の rlimit
    テストと同じパターン)。"""
    import resource as _resource

    calls: list[tuple] = []
    monkeypatch.setattr(_resource, "setrlimit",
                        lambda which, limits: calls.append((which, limits)))

    mission_worker._set_resource_limits(as_mb=64, nofile=32, fsize_mb=2)

    assert calls == [
        (_resource.RLIMIT_AS, (64 * 1024 * 1024, 64 * 1024 * 1024)),
        (_resource.RLIMIT_NOFILE, (32, 32)),
        (_resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024)),
        (_resource.RLIMIT_CORE, (0, 0)),
    ]


# ---- mission_protocol / _RagRpcProxy の残りの防御 (同上) ------------------


def test_seq_tracker_rejects_bool_as_seq():
    """`SeqTracker.check` の型検証。`bool` は `int` の派生なので
    `isinstance(seq, int)` だけでは素通りする (`True == 1`) — 明示的に
    弾く分岐が無いと `{"seq": true}` を送る壊れた親を受理してしまう。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    t = SeqTracker()
    with pytest.raises(ProtocolError, match="int"):
        t.check(True)
    with pytest.raises(ProtocolError, match="int"):
        SeqTracker().check("1")


def test_rag_rpc_proxy_rejects_rpc_id_mismatch():
    """`rpc_id` 照合。同時 1 件の同期 RPC でも、親が別要求の応答を返した
    場合に**それを結果として信用しない** (fail closed)。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    proxy = mission_worker._RagRpcProxy(
        lambda frame: None,
        lambda: {"type": "tool_rpc_result", "seq": 1, "rpc_id": "99",
                 "ok": True, "result": [{"title": "wrong"}]},
        SeqTracker(), SeqTracker())
    with pytest.raises(RuntimeError, match="rpc_id"):
        proxy.search_news("q")


def test_rag_rpc_proxy_raises_when_parent_closes_pipe():
    """親がパイプを閉じた (EOF = `read_frame` が None) 場合。None を結果と
    して返すと、ツールが「検索ヒット 0 件」と区別できない値を Mission に
    見せてしまう。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    proxy = mission_worker._RagRpcProxy(
        lambda frame: None, lambda: None, SeqTracker(), SeqTracker())
    with pytest.raises(RuntimeError, match="parent closed"):
        proxy.search_news("q")


def test_rag_rpc_proxy_advances_out_seq_across_successive_calls():
    """`_seq_next` がカウンタを前進させる。前進しないと 2 回目以降の
    `tool_rpc` が同じ seq で送られ、親の `SeqTracker` が重複として
    `ProtocolError` を投げる (RAG を 2 回使う Mission が必ず落ちる)。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    sent: list[dict] = []
    responses = iter([
        {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": True,
         "result": []},
        {"type": "tool_rpc_result", "seq": 2, "rpc_id": "2", "ok": True,
         "result": []},
    ])
    proxy = mission_worker._RagRpcProxy(
        sent.append, lambda: next(responses), SeqTracker(), SeqTracker())
    proxy.search_news("a")
    proxy.search_reflections("b")

    assert [f["seq"] for f in sent] == [1, 2]
    assert [f["name"] for f in sent] == ["search_news", "search_reflections"]


# ---------------------------------------------------------------------------
# レビュー 1 周目 (codex 主査 I-1〜I-3 / sonnet 副査 C1・C2・B・rlimit) の
# 反映で追加した回帰ピン。
# ---------------------------------------------------------------------------


class _ExitCalled(BaseException):
    """`os._exit` の代替。`main()` の `except Exception` に捕まらないよう
    `BaseException` を継承する (実プロセスでは戻ってこない呼び出しなので、
    テストでも「以降を実行しない」を再現する必要がある)。"""


def _fake_os_exit(monkeypatch):
    codes: list[int] = []

    def fake_exit(code):
        codes.append(code)
        raise _ExitCalled(code)

    monkeypatch.setattr(mission_worker.os, "_exit", fake_exit)
    return codes


class _FlakyStream:
    """指定した回数目の `write()` で失敗する fake。

    レビュー 2 周目 (codex): 旧版は失敗回に**書き込む前に** raise していた
    ため、実 I/O の曖昧性 (部分書込み後の失敗) をモデル化できていなかった。
    `partial_bytes` を指定すると「一部だけ書いてから失敗」を再現する。
    """

    def __init__(self, fail_writes: set[int], partial_bytes: int = 0) -> None:
        self.calls = 0
        self._fail = fail_writes
        self._partial = partial_bytes
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.calls += 1
        if self.calls in self._fail:
            if self._partial:
                self.buf += data[:self._partial]  # 部分書込み後に失敗
            raise BrokenPipeError("simulated pipe failure")
        self.buf += data

    def flush(self) -> None:
        pass


class _UnserializableOutput:
    """`json.dumps` できない値 (`MissionResult.output` に載せて serialize
    失敗を注入する)。"""


def test_main_reports_ready_false_on_malformed_handshake_json(
        monkeypatch, tmp_path):
    """codex I-1: 不正 JSON の handshake でも `ready: ok=False` を返す。

    旧実装は `read_frame` を外側 `try` の**前**で呼んでいたため、
    `JSONDecodeError` が素通しして traceback で異常終了し、**親からは
    起動 timeout と区別がつかなかった**。"""
    frames, rlimit_calls, registry_calls = _drive_main(
        monkeypatch, tmp_path, raw_stdin=b"{not-json}\n")

    assert len(frames) == 1
    assert frames[0]["type"] == "ready" and frames[0]["ok"] is False
    assert "ProtocolError" in frames[0]["error"]
    assert rlimit_calls == [] and registry_calls == []


def test_main_reports_ready_false_on_non_object_handshake(monkeypatch, tmp_path):
    """codex I-1: JSON としては妥当だがフレーム契約に反する入力 (配列)。"""
    frames, _, registry_calls = _drive_main(
        monkeypatch, tmp_path, raw_stdin=b'["handshake", 1]\n')

    assert len(frames) == 1
    assert frames[0]["ok"] is False
    assert "ProtocolError" in frames[0]["error"]
    assert registry_calls == []


def test_main_exits_without_retrying_when_result_write_fails(
        monkeypatch, tmp_path):
    """レビュー 2 周目 (codex): **transport 失敗の後に同じ seq で再送しない**。

    旧版のピンは「3 回目の write を失敗させ、内側 `except` が同じ seq=3 で
    failed result を送り直す」ことを期待していたが、これは「write 例外 ⇒
    wire に 1 バイトも出ていない」という**成立しない仮定**に依存していた。
    部分書込みの後に失敗していれば、再送は wire に壊れた行を作る。

    ここでは result の write が**部分書込みの後に**失敗する状況を注入し、
    子が即終了 (`os._exit(1)`) して**一切再送しない**ことを pin する。"""
    codes = _fake_os_exit(monkeypatch)
    stream = _FlakyStream({3}, partial_bytes=10)

    with pytest.raises(_ExitCalled):
        _drive_main(monkeypatch, tmp_path, out_stream=stream)

    assert codes == [1]
    # wire には ready(1) / event(2) と、途中で切れた 3 本目の断片だけ。
    # **その後ろに再送フレームが連結されていない**ことが本質。
    lines = bytes(stream.buf).split(b"\n")
    assert json.loads(lines[0])["type"] == "ready"
    assert json.loads(lines[1])["type"] == "event"
    assert len(lines) == 3          # 断片 1 個のみ (末尾に改行が無い)
    assert not lines[2].endswith(b"}")   # 完結した JSON 行ではない


def test_main_exits_without_retrying_when_result_flush_fails(
        monkeypatch, tmp_path):
    """同じ防御を別方向から壊す: `write` が全バイト受理した後に `flush` が
    失敗した場合。親が既に受理している可能性があるので、同じ seq の再送は
    **重複**になる。やはり再送せず終了する。"""
    codes = _fake_os_exit(monkeypatch)

    class _FlushFailsOnThird:
        def __init__(self):
            self.calls = 0
            self.buf = bytearray()

        def write(self, data):
            self.calls += 1
            self.buf += data

        def flush(self):
            if self.calls == 3:
                raise BrokenPipeError("flush failed after delivery")

    stream = _FlushFailsOnThird()
    with pytest.raises(_ExitCalled):
        _drive_main(monkeypatch, tmp_path, out_stream=stream)

    assert codes == [1]
    frames = [json.loads(l) for l in bytes(stream.buf).splitlines()]
    assert [f["type"] for f in frames] == ["ready", "event", "result"]
    assert [f["seq"] for f in frames] == [1, 2, 3]
    # seq=3 の result が 1 本だけ。再送していれば 2 本目が続く。
    assert sum(1 for f in frames if f["seq"] == 3) == 1


def test_main_reuses_seq_when_result_frame_cannot_be_serialized(
        monkeypatch, tmp_path):
    """レビュー 2 周目 (codex): **serialize 失敗は wire 未接触なので再送可**。

    `runner.run()` が JSON 化できない `output` を返すと `encode_frame` が
    `TypeError` を送出する — ストリームには触れていないので seq は未消費。
    内側 `except` が送る failed result は**同じ seq=3** で出なければならず、
    欠番も重複も生じない。これが「送出成功後に採番を進める」修正 (1 周目
    codex I-2) の本来の適用範囲である。"""
    class _BadOutputRunner(_FakeLocalRunner):
        def run(self, mission):
            from agentic_fx.runners.base import MissionResult
            self.kwargs["on_message"]({"role": "assistant", "content": "hi"})
            return MissionResult(status="completed",
                                 output={"bad": _UnserializableOutput()})

    # serialize 失敗では **プロセスを落とさない** ことも同時に pin する
    # (落とすと wire 未接触なのに Mission 失敗を報告できなくなる)。
    codes = _fake_os_exit(monkeypatch)
    frames, _, _ = _drive_main(monkeypatch, tmp_path, runner_cls=_BadOutputRunner)

    assert codes == []
    assert [f["type"] for f in frames] == ["ready", "event", "result"]
    assert [f["seq"] for f in frames] == [1, 2, 3]
    assert frames[2]["status"] == "failed"
    assert "TypeError" in frames[2]["error"]


def test_main_does_not_resend_ready_when_outer_except_is_reached_after_ready(
        monkeypatch, tmp_path):
    """1 周目 codex I-2 (外側 `except` の `seq: 1` ハードコード) の回帰ピン。

    result の serialize と、内側 `except` が送る failed result の serialize を
    **両方**失敗させると外側 `except` に到達する。旧実装はここで無条件に
    `{"type": "ready", "seq": 1, ...}` を送っていたため、既に送出済みの
    `ready(seq=1)` と重複した。"""
    class _BadOutputRunner(_FakeLocalRunner):
        def run(self, mission):
            from agentic_fx.runners.base import MissionResult
            self.kwargs["on_message"]({"role": "assistant", "content": "hi"})
            return MissionResult(status="completed",
                                 output={"bad": _UnserializableOutput()})

    # 内側 except の failed result も serialize 不能にする — error 文字列に
    # 直接は載らないので、encode_frame 自体を 2 回目以降失敗させる。
    real_encode = mission_worker.encode_frame
    state = {"n": 0}

    def flaky_encode(frame):
        if frame.get("type") == "result":
            state["n"] += 1
            if state["n"] <= 2:
                raise TypeError("cannot serialize result")
        return real_encode(frame)

    monkeypatch.setattr(mission_worker, "encode_frame", flaky_encode)

    codes = _fake_os_exit(monkeypatch)
    frames, _, _ = _drive_main(monkeypatch, tmp_path, runner_cls=_BadOutputRunner)

    assert codes == []
    assert sum(1 for f in frames if f["type"] == "ready") == 1
    assert [f["seq"] for f in frames] == [1, 2, 3]
    assert frames[2]["type"] == "result" and frames[2]["status"] == "failed"


def test_main_fails_closed_when_resource_limits_cannot_be_set(
        monkeypatch, tmp_path):
    """sonnet 副査: docstring が明記する「resource limit の設定失敗は
    fail closed」を実際に pin する。`_drive_main` は既定で常に成功する
    fake に差し替えているため、この経路は変異 26 件にも追加 12 本にも
    含まれておらず無防備だった。"""
    frames, rlimit_calls, registry_calls = _drive_main(
        monkeypatch, tmp_path, resource_limits_raise=True)

    assert len(rlimit_calls) == 1  # 呼ばれてはいる
    assert len(frames) == 1
    assert frames[0]["type"] == "ready" and frames[0]["ok"] is False
    assert "setrlimit" in frames[0]["error"]
    # Mission 実行へは一切進まない。
    assert registry_calls == []


def test_protect_protocol_stdout_redirects_fd1_to_stderr(monkeypatch):
    """sonnet 副査 B: `dup2` は実 subprocess なしで pin できる。

    `sys.stdout`/`sys.stderr` を実 pipe fd を持つ fake に差し替えると、
    実プロセスの fd 1 を触らずに「fd 1 への書込みが stderr へ流れる」
    「protocol_out は元の stdout へ書き続けられる」を観測できる。
    `dup2` を削除すると前者が成立せず red になる。"""
    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()
    os.set_blocking(r_out, False)
    os.set_blocking(r_err, False)

    class FakeStream:
        def __init__(self, fd):
            self._fd = fd

        def fileno(self):
            return self._fd

    monkeypatch.setattr(mission_worker.sys, "stdout", FakeStream(w_out))
    monkeypatch.setattr(mission_worker.sys, "stderr", FakeStream(w_err))

    protocol_out = mission_worker._protect_protocol_stdout()
    try:
        # 意図しない print 相当: fd 1 への書込みは stderr パイプへ流れる。
        os.write(w_out, b"leaked-print\n")
        assert os.read(r_err, 4096) == b"leaked-print\n"
        with pytest.raises(BlockingIOError):
            os.read(r_out, 4096)

        # protocol_out (dup した元の stdout) は本来の相手へ届く。
        protocol_out.write(b'{"type":"ready"}\n')
        protocol_out.flush()
        assert os.read(r_out, 4096) == b'{"type":"ready"}\n'
    finally:
        protocol_out.close()
        for fd in (r_out, w_out, r_err, w_err):
            try:
                os.close(fd)
            except OSError:
                pass


class _FakeLocalRunnerWithReason:
    """Task 3 (CP10) 用の `LocalRunner` 差し替え。reason 付き failed
    `MissionResult` を返す。`_FakeLocalRunner` と違い `on_message` は
    呼ばない (event フレームは本テストの対象外)。"""

    instances: list["_FakeLocalRunnerWithReason"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeLocalRunnerWithReason.instances.append(self)

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        return MissionResult(
            status="failed", output=None,
            reason="context exceeded: prompt 90010 tokens > "
                   "n_ctx 65536 (model=m)")


def test_main_puts_reason_in_result_frame_when_runner_sets_it(
        monkeypatch, tmp_path):
    """Task 3 / CP10: LocalRunner が MissionResult.reason を設定したら、
    子は result フレームにそれを載せる (worker_profile=trade 経路)。"""
    frames, _, _ = _drive_main(monkeypatch, tmp_path,
                               runner_cls=_FakeLocalRunnerWithReason)
    result_frame = frames[-1]
    assert result_frame["type"] == "result"
    assert result_frame["status"] == "failed"
    assert result_frame["reason"] == (
        "context exceeded: prompt 90010 tokens > n_ctx 65536 (model=m)")


def test_main_puts_reason_in_result_frame_for_improve_profile(
        monkeypatch, tmp_path):
    """Task 3 / CP10 の improve profile 側 (別コードパス — mission_worker.py
    の improve 分岐は trade 分岐と独立した result frame 構築コードを持つ)。"""
    # Landlock はプロセス生涯に不可逆。既存テストと同じ seam で bootstrap を
    # 止め、pytest プロセスを sandbox 化しない。
    # 命名は同ファイル冒頭の既存 import (`from agentic_fx import mission_worker`)
    # と既存テストに合わせる (新規テストだけ別名を持ち込まない)。
    monkeypatch.setattr(mission_worker, "_bootstrap_improve_profile",
                        lambda *args, **kwargs: None)

    def settings_mutator(settings_dict):
        pass  # improve は既定 settings のまま (backend=local)

    frames, _, _ = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides={"worker_profile": "improve",
                             "db_path": None, "plugins_dir": None},
        settings_mutator=settings_mutator,
        runner_cls=_FakeLocalRunnerWithReason)
    result_frame = frames[-1]
    assert result_frame["type"] == "result"
    assert result_frame["reason"] == (
        "context exceeded: prompt 90010 tokens > n_ctx 65536 (model=m)")

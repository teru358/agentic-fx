"""Mission worker 子プロセス本体 (プラン8) — python -m agentic_fx.mission_worker
として起動される。`runners/worker_runner.py` (親, Task 10) が spawn する唯一の
想定呼び出し元。

**起動順序は厳守** (plugin/worker.py と同じ規律):
1. handshake (最初の 1 行) を読む → **type/seq を検証する** (I2 対応:
   親→子方向専用の `in_seq` トラッカーで `type == "handshake"` かつ
   `seq == 1` を確認。不一致は `ProtocolError` — 以降の `try` 節に含めて
   fail closed で `ready: false` を返す)
2. `_set_pdeathsig` (親死亡時に OS が SIGTERM を配送) → 設定**直後**に
   `os.getppid()` を `expected_parent_pid` と照合し、不一致 (= 設定前に
   親が死んで再親付けされた) なら即終了する (設計書 §4.8 codex I2-4)
3. resource limit (RLIMIT_AS/NOFILE/FSIZE/CORE=0) を設定 — **設定失敗は
   fail closed** (worker_profile="trade" は全項目)。CPU 制限は付けない
   (Mission の消費は LLM 待ちの壁時計であり親の preemption が受け持つ —
   設計書 §4.5)
4. `build_mission_registry` でツール配線を組み立てる (RAG は `_RagRpcProxy`
   を注入。**`out_seq` を先に構築し、`_RagRpcProxy` と `on_message` の
   両方に同一インスタンスを共有させる** — CR-3 対応。独立した
   `SeqTracker` を `_RagRpcProxy` に持たせると、親側 `WorkerRunner` の
   `in_seq` (子→親の全フレーム種別を単一トラッカーで検証) と衝突し、
   RAG 検索ツールの初回呼出しで必ず `ProtocolError` になっていた)
5. `ready` を送出
6. `LocalRunner.run(mission)` を実行 (`on_message` が `event` フレームを
   送出)
7. `result` を送出して終了

**worker_profile="trade" 限定** (本プランのスコープ — improve profile は
Task 18 で bootstrap を拡張する)。`settings.runner.trade.backend` が
"local" 以外 (= "claude") の場合は `RuntimeError` で fail closed する
(ClaudeRunner は本プランでは実装しない — Global Constraints)。

**I4 対応 (実時計)**: 子は Mission 実行中の鮮度判定 (`get_signals` 等)
に `_build_clock()` (既定 `SystemClock()`) を使う。handshake 時刻で
`FixedClock` に固定すると、Mission 後半でも鮮度境界の基準時刻が進まず、
実時刻なら範囲外になったはずの古い signal が残り続ける fail-open になる
(fork レビュー I4)。テストは `mission_worker._build_clock` を
monkeypatch して `FixedClock` を注入できる (seam は残す)。
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, encode_frame, read_frame,
)

if TYPE_CHECKING:
    from agentic_fx.core.contracts import Clock

_PR_SET_PDEATHSIG = 1


def _build_clock() -> "Clock":
    """本番は実時計を使う (I4 対応)。テストはこの関数を monkeypatch して
    `FixedClock` を注入できる (seam)。"""
    from agentic_fx.core.contracts import SystemClock
    return SystemClock()


def _set_pdeathsig(sig: int) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_PDEATHSIG) failed: "
                             f"{os.strerror(errno)}")


def _set_resource_limits(*, as_mb: int, nofile: int, fsize_mb: int) -> None:
    import resource

    as_bytes = int(as_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _RagRpcProxy:
    """`news_tools.build(rag)`/`reflection_tools.build(conn, rag, pairs)`
    が要求する duck-type 契約 (`search_news`/`search_reflections`) のみを
    実装する (chromadb PersistentClient は多プロセス同時アクセス非対応 —
    設計書 §4.4)。`tool_rpc` は常に同時 1 件以下、同期ブロッキング待ち
    (設計書 §4.3)。

    `out_seq` (CR-3 対応) — 子→親方向 (`ready`/`event`/`tool_rpc`/
    `result`) の全フレームは `main()` が保持する単一の `SeqTracker` を
    共有する。`_RagRpcProxy` が自前の独立した `SeqTracker` を持つと、
    親側 `WorkerRunner.in_seq` (子→親の全フレーム種別を単一トラッカーで
    検証 — 設計書 §4.3 codex M2-1) の期待値と衝突し、RAG 検索ツールを
    使う Mission が初回呼出しで必ず `ProtocolError` になっていた
    (レビュー CR-3)。

    `in_seq` (I2 対応) — 親→子方向 (`tool_rpc_result`) 専用の受信検証
    トラッカー。`main()` が `handshake` の検証にも同じインスタンスを
    使う (親→子方向は 1 起点で共通)。**この共有の帰結として、本番では
    親が送る最初の `tool_rpc_result` の seq は 2 になる** (seq=1 は
    handshake が消費済み) — Task 10 の親側実装はこれに合わせること。"""

    def __init__(self, write_fn: Callable[[dict], None],
                read_fn: Callable[[], dict | None],
                out_seq: SeqTracker, in_seq: SeqTracker) -> None:
        self._write = write_fn
        self._read = read_fn
        self._out_seq = out_seq
        self._in_seq = in_seq
        self._rpc_counter = 0

    def _call(self, name: str, args: dict) -> Any:
        self._rpc_counter += 1
        rpc_id = str(self._rpc_counter)
        # レビュー 1 周目 (codex I-2): seq は**送出が成功してから**進める。
        # 先に消費すると、write/flush が失敗したときにその seq が wire に
        # 出ないまま欠番になり、次に成功したフレームを親の `SeqTracker` が
        # reject する。
        seq = self._out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
        self._write({"type": "tool_rpc", "seq": seq,
                     "rpc_id": rpc_id, "name": name, "args": args})
        self._out_seq._expected += 1  # noqa: SLF001
        response = self._read()
        if response is None:
            raise RuntimeError("parent closed the pipe while awaiting tool_rpc_result")
        # I2 対応: type/seq を検証してから中身を信用する。
        if response.get("type") != "tool_rpc_result":
            raise ProtocolError(
                f"expected tool_rpc_result, got {response.get('type')!r}")
        self._in_seq.check(response.get("seq"))
        if response.get("rpc_id") != rpc_id:
            raise RuntimeError(
                f"tool_rpc_result rpc_id mismatch: expected {rpc_id}, "
                f"got {response.get('rpc_id')!r}")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "rag rpc failed")))
        return response.get("result")

    def search_news(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_news", {"query": query, "n": n})

    def search_reflections(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_reflections", {"query": query, "n": n})


def _make_on_message(protocol_out: Any, out_seq: SeqTracker) -> Callable[[dict], None]:
    """`LocalRunner(on_message=...)` に渡すコールバックを組み立てる
    (I6 対応 — 独立関数に切り出してあるのは単体テストで `os._exit` を
    monkeypatch し、write 失敗時の fail-closed 経路を `main()` 全体を
    実行せずに検証するため)。"""
    def on_message(msg: dict) -> None:
        try:
            _send_frame(protocol_out, out_seq, {"type": "event", "message": msg})
        except Exception:  # noqa: BLE001 — I6 対応 (fail closed)
            # LocalRunner._sink (Task 6) は on_message の例外を握って
            # run() を継続する契約 — しかし event フレームを送れないまま
            # 実行を続けると、親は transcript の一部を永久に受け取れない
            # (レビュー I6)。fail-soft に「継続」させず、このプロセスを
            # 即座に終了する (親は EOF/予期しない終了として Mission を
            # 失敗させる — 既に壊れた状態で run() を続けても無意味)。
            #
            # レビュー 2 周目 (codex) 以降、transport 失敗は
            # `_write_frame_or_die` がプロセスごと落とすため、この節へ
            # 実際に到達するのは **serialize 失敗** (transcript の message
            # が JSON にならない) のとき。その場合 seq は未消費なので
            # 欠番にはならないが、送れなかった事実は変わらないので
            # 同じく fail closed にする。
            os._exit(1)
    return on_message


def _protect_protocol_stdout() -> Any:
    """JSON 行プロトコル専用の書き込み先を確保し、fd 1 (stdout) を fd 2
    (stderr) へ付け替えて返す (`plugin/worker.py:_protect_protocol_stdout`
    と同じパターン — import 済みライブラリの意図しない `print`/警告出力が
    プロトコルの JSON 1 行ストリームに混ざるのを防ぐ)。**何よりも先に**
    (`read_frame` より前) 呼ぶ — import 時の副作用による stdout 汚染も
    ここで防がれる対象に含める。
    """
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol_out


def main() -> None:
    protocol_out = _protect_protocol_stdout()

    # I2 対応: 親→子方向 (handshake/tool_rpc_result) 専用の受信検証
    # トラッカー。handshake は常に seq=1 (親の唯一の起動時送出)。
    in_seq = SeqTracker()
    # CR-3 対応: 子→親方向の ready/event/tool_rpc/result は 1 起点の単一
    # カウンタを共有する (設計書 §4.3 codex M2-1、親側 WorkerRunner.in_seq
    # がそう検証する)。レビュー 1 周目 (codex I-2) で `main()` の先頭へ
    # 移動した — 外側 `except` からも採番できる必要があるため。
    out_seq = SeqTracker()
    ready_sent = False
    try:
        # レビュー 1 周目 (codex I-1): handshake の読み取りを `try` の**内側**
        # へ移した。旧実装は `try` の外で `read_frame` を呼んでいたため、
        # 不正 JSON の handshake が `ProtocolError` を素通しさせて traceback
        # で異常終了し、**`ready: ok=False` を一切返さなかった** (親からは
        # 起動 timeout と区別がつかない)。EOF (None) は従来どおり正常終了。
        handshake = read_frame(sys.stdin.buffer)
        if handshake is None:
            return
        if handshake.get("type") != "handshake":
            raise ProtocolError(
                f"expected handshake, got {handshake.get('type')!r}")
        in_seq.check(handshake.get("seq"))

        expected_parent_pid = handshake["expected_parent_pid"]
        _set_pdeathsig(signal.SIGTERM)
        if os.getppid() != expected_parent_pid:
            # 設計書 §4.8 codex I2-4: prctl 設定前に親が死んで再親付け
            # されたレース。ready を送らずに即終了する (親の起動 timeout
            # がこれを検出する)。
            return
        settings_dict = handshake["settings"]
        worker_profile = handshake["worker_profile"]
        if worker_profile != "trade":
            raise RuntimeError(
                f"unsupported worker_profile in this plan: {worker_profile!r}")
        _set_resource_limits(
            as_mb=settings_dict["worker"]["child_as_mb"],
            nofile=settings_dict["worker"]["child_nofile"],
            fsize_mb=settings_dict["worker"]["child_fsize_mb"])

        from agentic_fx.config import Settings
        settings = Settings.model_validate(settings_dict)
        if settings.runner.trade.backend != "local":
            raise RuntimeError(
                f"runner.trade.backend={settings.runner.trade.backend!r} is "
                "not supported by mission_worker in this plan (ClaudeRunner "
                "is Plan 9 scope) — fail closed")

        from agentic_fx.store.db import connect_readonly
        from agentic_fx.tools import plugin_loader
        from agentic_fx.tools.mission_registry import build_mission_registry
        from agentic_fx.activity import ActivityLog

        conn = connect_readonly(Path(handshake["db_path"]))
        # I4 対応: 本番は実時計を使う (handshake["now"] で FixedClock に
        # 固定すると、Mission 後半でも鮮度判定の基準時刻が進まず
        # get_signals 等の鮮度検証が fail-open になる — レビュー I4)。
        clock = _build_clock()
        # 子は自身の scratch workdir に activity.log を持つ (§4.6 のとおり
        # econ.upcoming() は self.activity に触れないため実際には書かれ
        # ないが、EconCalendar のコンストラクタ契約を満たすためだけに
        # 必要 — mission_registry.py の docstring 参照)。
        activity = ActivityLog(Path.cwd() / "activity.log")
        plugins_dir = (Path(handshake["plugins_dir"])
                       if handshake.get("plugins_dir") else None)
        approved = (plugin_loader.approved_plugins(conn, plugins_dir)
                   if plugins_dir is not None else [])

        # CR-3 対応: `main()` 冒頭で構築した out_seq を、_RagRpcProxy と
        # on_message (event フレーム送出) の両方に**同一インスタンス**で
        # 共有させる。
        registry = build_mission_registry(
            "trade", conn, settings, clock,
            _RagRpcProxy(
                lambda frame: _write_frame_or_die(protocol_out, frame),
                lambda: read_frame(sys.stdin.buffer),
                out_seq, in_seq),
            activity=activity, indicator_plugins=approved, readonly=True)

        from agentic_fx.runners.base import Mission
        from agentic_fx.runners.local_runner import LocalRunner

        mission = Mission(**handshake["mission"])

        on_message = _make_on_message(protocol_out, out_seq)

        runner = LocalRunner(
            base_url=settings.llama_swap.base_url,
            model=settings.runner.trade.model, registry=registry,
            on_message=on_message)

        _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
        ready_sent = True

        try:
            result = runner.run(mission)
            _send_frame(protocol_out, out_seq, {
                "type": "result",
                "status": result.status, "output": result.output})
        except Exception as exc:  # noqa: BLE001 — 必ず result を送る
            _send_frame(protocol_out, out_seq, {
                "type": "result", "status": "failed", "output": None,
                "error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:  # noqa: BLE001 — ready 送出前の失敗も報告する
        try:
            # レビュー 1 周目 (codex I-2): 旧実装は無条件に
            # `{"type": "ready", "seq": 1, ...}` を送っていた。`ready`
            # (seq=1) を送出**済み**でこの節に到達する経路が実在する
            # (内側 except 自身の送出が失敗した場合) ため、親の
            # `SeqTracker` から見て seq=1 の**重複**になっていた。
            # 送出済みなら `result: failed` として報告する。
            if ready_sent:
                _send_frame(protocol_out, out_seq, {
                    "type": "result", "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
            else:
                _send_frame(protocol_out, out_seq, {
                    "type": "ready", "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 — パイプが壊れていれば諦める
            pass


def _write_frame_or_die(protocol_out: Any, frame: dict) -> None:
    """1 フレームを protocol stream へ書く。**transport 失敗なら即プロセス終了**。

    レビュー 2 周目 (codex): `write`/`flush` の例外は「フレームが wire に
    1 バイトも出ていない」ことを**保証しない**。部分書込みの後に失敗して
    いれば、同じ seq で別フレームを送り直すと `{"type":...` の途中に別の
    JSON が連結されて**行が壊れる**。flush がデータを相手へ渡した後に失敗
    したのなら、親は既に最初のフレームを受理しており、再送は**重複**に
    なる。どちらが起きたかは呼び出し側から判定できない。

    したがって transport 失敗は回復不能として扱い、**再送せずに子を即座に
    終了する** (fail closed)。親は EOF / 異常終了として Mission を失敗
    させる — これは `_make_on_message` (I6) が既に採っている方針と同じ。

    serialize 失敗 (`encode_frame` の `TypeError` 等) は**ここへ来る前に**
    送出され、ストリームには触れない。呼び出し側はその場合だけ同じ seq で
    別フレームを送り直してよい。
    """
    line = encode_frame(frame)  # serialize 失敗はここ (wire 未接触 → 再送可)
    try:
        protocol_out.write(line)
        protocol_out.flush()
    except Exception:  # noqa: BLE001 — 配信有無が不明なので回復を試みない
        os._exit(1)


def _send_frame(protocol_out: Any, out_seq: SeqTracker, frame: dict) -> None:
    """子→親のフレームを 1 件送出する (`seq` はここで採番して付す)。

    **カウンタは送出が成功してから進める** (レビュー 1 周目 codex I-2)。
    旧実装 (`_next_seq`) は serialize/write/flush より**前**に消費していた
    ため、送出が失敗するとその seq が wire に出ないまま欠番になり、次に
    成功したフレームを親の `SeqTracker` が `ProtocolError` として reject
    した。実測: `runner.run()` の戻り値が壊れていて result フレームの
    構築中に落ちると、wire 上は `ready(1)` の次が `result(3)` になった。

    「送出が成功していないなら seq を進めない」が安全なのは **serialize
    失敗のときだけ** — transport 失敗は `_write_frame_or_die` がプロセス
    ごと落とすため、そもそも呼び出し側に戻らない (レビュー 2 周目 codex)。

    `SeqTracker` を採番器として流用する意図は Task 7 本文のとおり
    (「次に来る/送り出すべき値」の意味が受信検証と送信採番で一致する)。
    """
    payload = dict(frame)
    payload["seq"] = out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
    _write_frame_or_die(protocol_out, payload)
    out_seq._expected += 1  # noqa: SLF001


if __name__ == "__main__":
    main()

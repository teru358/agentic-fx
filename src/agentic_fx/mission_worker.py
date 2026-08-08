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
    ProtocolError, SeqTracker, read_frame, write_frame,
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
        self._write({"type": "tool_rpc", "seq": self._seq_next(),
                     "rpc_id": rpc_id, "name": name, "args": args})
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

    def _seq_next(self) -> int:
        # 子→親方向の ready/event/tool_rpc/result は同じカウンタ
        # (main() から渡される out_seq) を共有する (CR-3 対応 — 単一
        # スレッドで動くため送出順=seq 順が自然に一致する)。WorkerRunner
        # (Task 10) 側の SeqTracker.check() と対称に、ここでは単に「次の
        # 値」を払い出すだけの単純カウンタとして使う (受信側の検証責務は
        # 親が持つ — 子は自分の送出 seq を数えるだけ)。
        n = self._out_seq._expected  # noqa: SLF001 — 同一モジュール内の協調実装
        self._out_seq._expected += 1
        return n

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
            write_frame(protocol_out, {
                "type": "event", "seq": _next_seq(out_seq), "message": msg})
        except Exception:  # noqa: BLE001 — I6 対応 (fail closed)
            # LocalRunner._sink (Task 6) は on_message の例外を握って
            # run() を継続する契約 — しかしここで write_frame が失敗
            # すると out_seq だけが消費され、次に成功する event フレーム
            # の seq が欠番になり親の SeqTracker が ProtocolError を
            # 送出する (レビュー I6)。fail-soft に「継続」させず、
            # このプロセスを即座に終了する (親は EOF/予期しない終了
            # として Mission を失敗させる — 既に壊れた状態で
            # LocalRunner.run() を続けても無意味)。
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
    handshake = read_frame(sys.stdin.buffer)
    if handshake is None:
        return

    # I2 対応: 親→子方向 (handshake/tool_rpc_result) 専用の受信検証
    # トラッカー。handshake は常に seq=1 (親の唯一の起動時送出)。
    in_seq = SeqTracker()
    try:
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

        # CR-3 対応: out_seq を先に構築し、_RagRpcProxy と on_message
        # (event フレーム送出) の両方に同一インスタンスを共有させる —
        # 子→親方向の ready/event/tool_rpc/result は 1 起点の単一
        # カウンタでなければならない (設計書 §4.3 codex M2-1、親側
        # WorkerRunner.in_seq がそう検証する)。
        out_seq = SeqTracker()

        registry = build_mission_registry(
            "trade", conn, settings, clock,
            _RagRpcProxy(
                lambda frame: write_frame(protocol_out, frame),
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

        write_frame(protocol_out, {
            "type": "ready", "seq": _next_seq(out_seq), "ok": True})

        try:
            result = runner.run(mission)
            write_frame(protocol_out, {
                "type": "result", "seq": _next_seq(out_seq),
                "status": result.status, "output": result.output})
        except Exception as exc:  # noqa: BLE001 — 必ず result を送る
            write_frame(protocol_out, {
                "type": "result", "seq": _next_seq(out_seq),
                "status": "failed", "output": None,
                "error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:  # noqa: BLE001 — ready 送出前の失敗も報告する
        try:
            write_frame(protocol_out, {
                "type": "ready", "seq": 1, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 — パイプが壊れていれば諦める
            pass


def _next_seq(tracker: SeqTracker) -> int:
    n = tracker._expected  # noqa: SLF001 — 送出側は検証ではなく採番に使う
    tracker._expected += 1
    return n


if __name__ == "__main__":
    main()

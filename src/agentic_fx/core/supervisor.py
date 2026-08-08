"""MissionSupervisor — 容量 1 のジョブスロットで Mission 実行を単一スレッド
直列実行する (プラン8, 設計書 §3.3)。

直列性の保証は「supervisor スレッドが 1 本」という構造に置く。`try_submit`
は内部 lock の下で「実行中 + 予約済み」の有無を判定し受理/拒否を即座に
返す原子的契約 (TOCTOU 封鎖)。ジョブは 2 種:
`"trade"` (kwargs={"trigger"} — 完了直後に reflection バッチを自動連鎖) /
`"ask"` (kwargs={"question"} — Future で結果を返す)。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable

_log = logging.getLogger("agentic_fx.supervisor")

# 裁定書 F-3 (CR-1): heartbeat ポンプの touch 間隔。Task 19 の
# heartbeat_grace_sec はこの値に対してのみ余裕 (目安 6 倍程度) を見ればよい。
_HEARTBEAT_PUMP_INTERVAL_SEC = 5.0


class MissionSupervisor:
    def __init__(self, *, trade_fn: Callable[[str], object],
                reflection_fn: Callable[[], object],
                ask_fn: Callable[[str], str],
                heartbeat_pump_interval_sec: float = _HEARTBEAT_PUMP_INTERVAL_SEC,
                ) -> None:
        self._trade_fn = trade_fn
        self._reflection_fn = reflection_fn
        self._ask_fn = ask_fn
        # 裁定書 F-3 (CR-1): テストが real-sleep 予算 (数秒以内) を守れる
        # よう、pump 間隔を注入可能にする (既定は本番用の定数)。
        self._heartbeat_pump_interval_sec = heartbeat_pump_interval_sec
        self._lock = threading.Lock()
        self._busy = False
        self._queue: "queue.Queue[tuple[str, dict, Future] | None]" = \
            queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.heartbeat: float = time.monotonic()
        # 裁定書 F-3 (CR-1) advisor 指摘反映: heartbeat ポンプは
        # 「supervisor スレッドが生きている」ことしか示さず、_dispatch が
        # 呼ぶ先 (broker.submit 等) が真にデッドロックした場合はポンプが
        # 動き続けてしまい heartbeat だけでは検出できない (fail-open の
        # 穴)。`busy_since` (dispatch 開始時刻、非 busy 時は None) を
        # 別属性として公開し、Task 19 watchdog がこれと設定由来の上限
        # (dispatch_ceiling_sec) を突き合わせて「heartbeat は新鮮だが
        # dispatch が上限を超えて戻ってこない」を独立に検出できるように
        # する (heartbeat 鮮度チェックと busy_since 上限チェックは別軸)。
        self.busy_since: float | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="afx-supervisor")
        self._thread.start()

    def try_submit(self, kind: str, **kwargs) -> Future | None:
        with self._lock:
            if self._busy:
                return None
            self._busy = True
            future: Future = Future()
            self._queue.put((kind, kwargs, future))
            return future

    def shutdown(self, *, drain_exc: Exception) -> None:
        """新規受付停止 + queue 内の未着手ジョブを例外完了させる (ブロック
        しない — 実行中ジョブの完了待ちは呼び出し側の join() の責務)。"""
        self._stop_event.set()
        self.fail_pending(exc=drain_exc)

    def fail_pending(self, *, exc: Exception) -> None:
        """queue 内 (まだディスパッチされていない) の pending Future を
        `exc` で例外完了させる。supervisor スレッド死亡時に watchdog
        (Task 19) が呼ぶ経路と shutdown の両方から共有される。"""
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return
        if item is not None:
            _, _, future = item
            if not future.done():
                future.set_exception(exc)
            with self._lock:
                self._busy = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 内部 -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.heartbeat = time.monotonic()
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            kind, kwargs, future = item
            # レビュー反映1回目 (裁定書 F-3 / CR-1): _dispatch は trade+
            # reflection 連鎖で数百秒に及びうる (mission.timeout_sec +
            # worker_grace_sec を trade/reflection 各回で消費しうる) —
            # while ループ先頭でしか heartbeat を touch しないと、watchdog
            # の heartbeat_grace_sec (Task 19) を通常の Mission サイクル
            # だけで超過し、誤って fatal 判定される。dispatch 実行中も
            # 別スレッドで heartbeat を touch し続ける「ポンプ」を回す。
            pump_stop = threading.Event()
            pump = threading.Thread(
                target=self._pump_heartbeat, args=(pump_stop,),
                daemon=True, name="afx-supervisor-heartbeat-pump")
            pump.start()
            self.busy_since = time.monotonic()
            try:
                result = self._dispatch(kind, kwargs)
                if not future.cancelled():
                    future.set_result(result)
            except Exception as e:  # noqa: BLE001 — supervisor スレッドを殺さない
                _log.exception("mission job %r failed", kind)
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                pump_stop.set()
                with self._lock:
                    self._busy = False
                self.busy_since = None
                pump.join(timeout=1.0)

    def _pump_heartbeat(self, stop: threading.Event) -> None:
        """裁定書 F-3 (CR-1): `_dispatch` 実行中も `heartbeat` を
        `_HEARTBEAT_PUMP_INTERVAL_SEC` 間隔で touch し続ける。

        **このポンプ単体は fail-open の穴を持つ**: `pump.daemon=True` の
        スレッドは `_run` の親スレッド (supervisor スレッド) の生死とは
        独立に走り続けるため、`_dispatch` 内部 (`trade_fn`/`reflection_fn`
        が呼ぶ broker.submit・DB 書込等) が genuine にデッドロックしても
        ポンプは `heartbeat` を touch し続けてしまい、`heartbeat` の鮮度
        だけを見る監視では検出できない。**したがってこのポンプは
        `heartbeat_grace_sec` (Task 19) を小さく保つためだけに存在し、
        「supervisor が壊れていないか」の判定は `heartbeat` 鮮度チェック
        単独では完結させない** — Task 19 watchdog は `busy_since`
        (dispatch 開始時刻) と設定由来の `dispatch_ceiling_sec` を突き合わせる
        **独立した第二の軸**で「heartbeat は新鮮だが dispatch が上限を
        超えて戻ってこない」を検出する (Task 19 参照)。この 2 軸の組合せ
        で初めて「grace は Mission 所要時間に非依存」かつ「回復不能な
        ハングは必ず検出される」の両方が成立する。
        """
        while not stop.wait(self._heartbeat_pump_interval_sec):
            self.heartbeat = time.monotonic()

    def _dispatch(self, kind: str, kwargs: dict) -> object:
        if kind == "trade":
            trade_result = self._trade_fn(kwargs["trigger"])
            reflection_count = self._reflection_fn()
            return {"trade": trade_result,
                   "reflection_count": reflection_count}
        if kind == "ask":
            return self._ask_fn(kwargs["question"])
        raise ValueError(f"unknown job kind: {kind!r}")

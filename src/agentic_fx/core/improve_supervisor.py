"""ImproveSupervisor — 改善レーンの別スロット群 (設計書 §3.1、裁定6:
MissionSupervisor の一般化ではなく別クラス)。

接続所有 (ownership diagram はプラン文書 §9.5 参照):
- Tx-0 (missions.start + improve_runs.start + slot claim) は
  `self._improve_loop.prepare` (Task 10 の `ImproveLoop`) が専用接続で
  行う。`ImproveSupervisor` はこの接続を持たない (R-i2)。
- `_launch_slot` 自身は `mark_running` のときだけ自分専用の write 接続を
  開き、`finally` で close する。スレッド間で共有しない。
- RPC dispatcher (WorkerRunner 内、Task 4/7) は自スレッド内で読取専用
  接続を開閉する。台帳 (`ImproveRpcLedger`) だけが両者の橋。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_fx.core.scheduler import latest_scheduled_occurrence, period_key_of
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves

if TYPE_CHECKING:
    from agentic_fx.activity import ActivityLog
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock
    from agentic_fx.loops.improve_loop import ImproveLoop

_log = logging.getLogger("agentic_fx.improve_supervisor")

_MAX_SPAWN_ATTEMPTS = 2  # 初回 + 再試行 1 回 (設計書 §3.1 手順⑥)


class ImproveSupervisor:
    def __init__(self, *, capacity: int, root: Path, settings: "Settings",
                 clock: "Clock", db_path: Path,
                 stop_event: threading.Event,
                 activity: "ActivityLog | None" = None) -> None:
        self._capacity = capacity
        self._root = root
        self._settings = settings
        self._clock = clock
        self._db_path = db_path
        self._stop_event = stop_event
        # E4 裁定 (2026-08-25): `process_expired_approvals` の name 欠落
        # payload に対する activity ERROR 記録を、tick() 経由の呼び出し
        # (service.py 起動時 reconcile とは独立に毎 tick 実行) でも書ける
        # ようにする (既定 None — 既存呼び出し元との後方互換)。
        self._activity = activity
        # `ImproveLoop` (Task 10 の産物)。build_app 配線時 (10.12 節) に
        # 実インスタンスへ差し替える。None のままだと `_launch_slot` は
        # AttributeError で失敗する — Task 9 単独では未配線が正しい状態。
        self._improve_loop: "ImproveLoop | None" = None
        self._conn_for_test = None  # pytest シーム。本番は常に None。
        self._launch_lock = threading.Lock()
        self._active_threads: list[threading.Thread] = []

    # ---- 公開 API ----------------------------------------------------

    def tick(self, now: datetime) -> None:
        if self._stop_event.is_set():
            return
        # B-7 (裁定1): plugin approval の期限切れ処理は lock 外・毎 tick 実行
        # (プラン10 Task11g Step6b — 改善 loop が長時間止まっている間に溜まった
        # 期限切れ pending を取りこぼさないため、service.py 起動時 reconcile
        # とは独立にここでも process_expired_approvals を呼ぶ)。
        from agentic_fx.plugin import switch
        expire_conn = db_mod.connect(self._db_path)
        try:
            switch.process_expired_approvals(
                expire_conn, plugins_root=self._root / "plugins", now=now,
                activity=self._activity)
        finally:
            expire_conn.close()
        s = self._settings.schedule
        occurrence = latest_scheduled_occurrence(
            now, cadence=s.improve, at=s.improve_at,
            display_timezone=self._settings.display_timezone)
        period_key = period_key_of(occurrence, cadence=s.improve)
        conn = self._conn()
        owns = self._conn_for_test is None
        try:
            open_slots = self._capacity - self._running_slot_count(conn)
            m = min(self._settings.improve.parallel, max(open_slots, 0))
            if m == 0:
                return
            created = improve_waves.create_wave_and_slots(
                conn, period_key=period_key, now=now, expected=m, commit=True)
            if not created:
                return
            pending_ks = list(range(m))
        finally:
            if owns:
                conn.close()
        for k in pending_ks:
            self._spawn_slot_thread(period_key, k)

    def submit_manual(self) -> int:
        """手動 one-shot。slot/wave 行を作らず M=1 で全バックログを担当
        させる (§8.1-該当、9.7 節「improve」コマンドから呼ばれる)。
        wave slot が無いため on_ready コールバックは不要 (mark_running する
        対象が無い) — `prepare(on_ready=)` を省略すると既定 `None` になる。
        # precheck 2026-08-23 wave3: RW1/RW2 改訂 — `on_ready=None` でも
        # 子は go を待つ (§10.9b Step 12-4b/RW6)。WorkerRunner が go を
        # 送る条件は `on_ready is not None` ではなく `worker_profile`
        # そのものなので (Interfaces 節「Task 1 が produces」ブロック参照)、
        # ここで on_ready を渡さなくても子は正しく go を受け取って進む。
        """
        now = self._clock.now()
        mission, ctx, runner = self._improve_loop.prepare(
            slot_key=None, now=now)
        result = runner.run(mission)
        self._improve_loop.commit(mission=mission, ctx=ctx, result=result,
                                  now=self._clock.now())
        return ctx.mission_id

    def shutdown(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float) -> None:
        """検収 B5 (2026-08-22): 全スレッドで**共有する 1 つの deadline**
        (`monotonic() + timeout`) を切り、残余時間を各スレッドへ配る。
        修正前は各スレッドへ `timeout` を丸ごと渡していたため、最悪
        `len(_active_threads) × timeout` を消費しえた
        (`service.py:1203-1209` の I-3 不変条件 — join budget は
        watchdog ceiling と同じ値を共有する構造でなければならない — に
        抵触する)。あわせて `_active_threads` から終了済みスレッドを
        prune する (tick 時 `_spawn_slot_thread` でも行うが、join 時にも
        念のため行う — 単調増加を防ぐ)。"""
        deadline = time.monotonic() + timeout
        with self._launch_lock:
            threads = list(self._active_threads)
        for t in threads:
            remaining = deadline - time.monotonic()
            t.join(timeout=max(remaining, 0.0))
        with self._launch_lock:
            self._active_threads = [
                t for t in self._active_threads if t.is_alive()]

    # ---- 内部 ---------------------------------------------------------

    def _spawn_slot_thread(self, period_key: str, k: int) -> None:
        t = threading.Thread(
            target=self._launch_slot, args=(period_key, k), daemon=True,
            name=f"afx-improve-slot-{period_key}-{k}")
        with self._launch_lock:
            # 検収 B5: tick 時にも終了済みスレッドを prune し、
            # `_active_threads` が単調増加しないようにする。
            self._active_threads = [
                s for s in self._active_threads if s.is_alive()]
            self._active_threads.append(t)
        t.start()

    # precheck 2026-08-22 wave2: T10-B2 R-D1 実装時改訂 — 3相 API
    # (spawn/send_go/run_to_completion、非実在) を on_ready 方式へ書き換え。
    # `_handle_pre_ready_failure` (spawn_attempts による retry/failed 分岐、
    # 9.4 節) の意味論はそのまま維持する — `runner.run()` が **on_ready が
    # 一度も呼ばれずに** 完走以外の結果を返した場合 (pre-ready 失敗、子の
    # spawn 自体が失敗) だけ retry 経路へ倒し、on_ready 到達後 (= 'running'
    # へ遷移済み) の失敗は通常の `ImproveLoop.commit` 終端 (_finalize_failed_
    # mission、slot を running→failed) へ渡す — 「実行が始まった後の失敗」
    # を retry してしまうと二重実行になるため区別する。
    # I2(b) 是正 (プラン10 束D round1、ユーザー裁定 D①、2026-08-28):
    # 設計書 §3.1⑥/§8.1-19 (「pre-ready の失敗は同一プロセス内でのみ slot
    # を reserved に戻して再 claim、spawn_attempts は claim ごとに +1、
    # 失敗時に < 2 なら reserved (初回+再試行1回)、それ以外は failed」)
    # を、プラン 9.5 節 RW3 が実装しないまま (1 回きりの
    # prepare→run→commit) 据え置いていた欠陥を是正する。`_launch_slot`
    # 自身が同一プロセス内で再 claim (`prepare` の再呼び出し) → 再 spawn
    # する — `_MAX_SPAWN_ATTEMPTS` (=2) が実際に機能する形。
    def _launch_slot(self, period_key: str, k: int) -> None:
        # mission_id はまだ無い。Tx-0 (missions.start + improve_runs.start +
        # slot claim reserved→claimed) は self._improve_loop.prepare が
        # 一体で行う (統合裁定 R-i2、Task 10 の 10.2 節が正)。
        now = self._clock.now()
        while True:
            reached_running = False

            def _on_ready(frame: dict) -> None:
                nonlocal reached_running
                conn = self._conn()
                owns = self._conn_for_test is None
                try:
                    improve_waves.mark_running(
                        conn, period_key=period_key, k=k,
                        now=self._clock.now(), commit=True)
                finally:
                    if owns:
                        conn.close()
                reached_running = True

            mission, ctx, runner = self._improve_loop.prepare(
                slot_key=(period_key, k), now=now, on_ready=_on_ready)
            result = runner.run(mission)
            if reached_running or result.status == "completed":
                # on_ready 到達後 (= running へ遷移済み) の失敗、または
                # 完走 — 通常の commit 終端へ渡す。retry しない (二重実行
                # を避ける、コメント上部の区別どおり)。
                self._improve_loop.commit(
                    mission=mission, ctx=ctx, result=result,
                    now=self._clock.now())
                return
            # pre-ready 失敗 (on_ready 未到達) — I2b: 巻き戻し
            # (`_handle_pre_ready_failure`) は `commit()` より**前**に完了
            # させ、`commit()` には `slot_terminalize=False` を渡す。
            # `mark_terminal` は status ガードの無い無条件 UPDATE なので、
            # 先に revert した `reserved` を commit の無条件終端が潰す
            # のを防ぐ (I2 の主害)。
            should_retry = self._handle_pre_ready_failure(period_key, k)
            self._improve_loop.commit(
                mission=mission, ctx=ctx, result=result,
                now=self._clock.now(), slot_terminalize=False)
            if not should_retry:
                return
            now = self._clock.now()

    def _handle_pre_ready_failure(self, period_key: str, k: int) -> bool:
        """戻り値: `True` = slot を `reserved` へ戻した (呼び出し元は
        再 claim/再 spawn すること)。`False` = 再試行上限に達し `failed`
        へ収束した (呼び出し元は retry しない)。"""
        conn = self._conn()
        owns = self._conn_for_test is None
        try:
            now = self._clock.now()
            row = conn.execute(
                "SELECT spawn_attempts FROM improve_wave_slots "
                "WHERE wave_period_key=? AND k=?", (period_key, k)).fetchone()
            attempts = row["spawn_attempts"]
            if attempts < _MAX_SPAWN_ATTEMPTS:
                improve_waves.revert_to_reserved(
                    conn, period_key=period_key, k=k, now=now, commit=True)
                return True
            improve_waves.mark_slot_failed(
                conn, period_key=period_key, k=k, now=now, commit=True)
            return False
        finally:
            if owns:
                conn.close()

    def _running_slot_count(self, conn) -> int:
        row = conn.execute(
            "SELECT count(*) c FROM improve_wave_slots "
            "WHERE status IN ('claimed','running')").fetchone()
        return row["c"]

    def _conn(self):
        if self._conn_for_test is not None:
            return self._conn_for_test
        # 本番: slot ごとに専用接続 (共有しない)。close は呼び出し元の
        # finally が行う。
        return db_mod.connect(self._db_path)

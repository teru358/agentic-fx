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

from agentic_fx.activity import Category
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
        # round2 #3 追加 pin (2026-08-29、verified-round2.md #3): 設定検証
        # (config.py の ScheduleSettings._check_improve_at) を入れても
        # 「tick が例外で黙って死ぬ」構造自体は残る (scheduler_thread の
        # `except Exception: _log.exception("tick failed")` は技術ログのみ
        # で activity/notifier には出ない)。ここで一度だけ activity へ
        # 1 行残してから re-raise する (呼び出し元の技術ログ記録は妨げない)。
        try:
            occurrence = latest_scheduled_occurrence(
                now, cadence=s.improve, at=s.improve_at,
                display_timezone=self._settings.display_timezone)
        except Exception:
            _log.exception("ImproveSupervisor.tick: latest_scheduled_occurrence "
                           "failed for cadence=%r at=%r", s.improve, s.improve_at)
            if self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "improve_tick_schedule_error",
                    f"cadence={s.improve} at={s.improve_at!r}")
            raise
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
        try:
            self._improve_loop.commit(
                mission=mission, ctx=ctx, result=result, now=self._clock.now())
        except Exception as exc:
            _log.exception(
                "improve commit raised for manual mission_id=%s — "
                "compensating", ctx.mission_id)
            self._improve_loop.compensate_commit_failure(
                ctx=ctx, now=self._clock.now(), exc=exc,
                slot_terminalize=True)
            raise
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

    def _run_slot_thread(self, period_key: str, k: int) -> None:
        # round2 #5 是正: `_launch_slot` が re-raise した後の default
        # excepthook (stderr のみ) では activity にも notifier にも残らない。
        # `MissionSupervisor._run` (兄弟) が持つ try/except と対称にする —
        # 本体の補償 (`compensate_launch_failure`) は既に `_launch_slot`
        # 側で行っているので、ここは observability のためだけの 1 行。
        try:
            self._launch_slot(period_key, k)
        except BaseException:
            _log.exception(
                "improve slot thread crashed for %s/%s", period_key, k)
            if self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "improve_slot_thread_crashed",
                    f"period_key={period_key} k={k}")

    def _spawn_slot_thread(self, period_key: str, k: int) -> None:
        t = threading.Thread(
            target=self._run_slot_thread, args=(period_key, k), daemon=True,
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
                # C1 是正 (プラン10 束D round1、verified-codex-round1.md、
                # 2026-08-28): `mark_running` の CAS 戻り値を消費する —
                # 従来は無条件に `reached_running = True` としていたため、
                # CAS が rowcount=0 (slot が既に `claimed` でなくなって
                # いた) でも「親が running を commit した」ことにして子の
                # 実行を承認していた (設計 §3.1③ の authorization 順序の
                # 破れ)。in-process の再現経路は無いが (段0 D03/D09/D20 と
                # 同じクラス、fail-open ガードの欠落)、修正コストが極小
                # なので塞ぐ。CAS 失敗時は例外で fail closed する —
                # `WorkerRunner` は `on_ready` の例外を子の `_escalate_kill`
                # + `failed` 正規化として扱う
                # (`test_on_ready_exception_reverts_to_reserved_then_failed`
                # が既に踏んでいる経路)。
                nonlocal reached_running
                conn = self._conn()
                owns = self._conn_for_test is None
                try:
                    ok = improve_waves.mark_running(
                        conn, period_key=period_key, k=k,
                        now=self._clock.now(), commit=True)
                finally:
                    if owns:
                        conn.close()
                if not ok:
                    raise RuntimeError(
                        f"mark_running CAS failed for slot "
                        f"({period_key!r},{k}) — slot is not 'claimed' any "
                        "more; refusing to authorize the child")
                reached_running = True

            mission, ctx, runner = self._improve_loop.prepare(
                slot_key=(period_key, k), now=now, on_ready=_on_ready)
            # round2 #5 是正 (2026-08-29): I3/I2(b)/C1 のどの窓にも属さない
            # 「prepare() 成功後、runner.run() 自体が例外を投げる」第 4 の窓
            # (verified-round2.md #5)。WorkerRunner.run は shutil.copytree /
            # subprocess.Popen を try/except 無しで実行するため ENOSPC /
            # EMFILE 等が直撃しうる。無ガードだと mission(running)/
            # run(未終端)/slot(claimed) が dangling のまま残り、
            # `_running_slot_count` が容量を恒久的に食い潰す
            # (既定 improve.parallel=1 では改善ループが再起動まで停止する)。
            # I3 と同じ所有者 (mission_id/run_id/slot_key を知る `ctx`) から
            # 1 tx で終端化してから re-raise する。
            try:
                result = runner.run(mission)
            except BaseException:
                _log.exception(
                    "improve runner.run raised for slot %s/%s — "
                    "compensating (capacity leak guard)", period_key, k)
                self._improve_loop.compensate_launch_failure(
                    ctx=ctx, now=self._clock.now())
                raise
            if reached_running or result.status == "completed":
                # on_ready 到達後 (= running へ遷移済み) の失敗、または
                # 完走 — 通常の commit 終端へ渡す。retry しない (二重実行
                # を避ける、コメント上部の区別どおり)。
                try:
                    self._improve_loop.commit(
                        mission=mission, ctx=ctx, result=result,
                        now=self._clock.now())
                except Exception as exc:
                    _log.exception(
                        "improve commit raised for slot %s/%s — compensating",
                        period_key, k)
                    self._improve_loop.compensate_commit_failure(
                        ctx=ctx, now=self._clock.now(), exc=exc,
                        slot_terminalize=True)
                    raise
                return
            # pre-ready 失敗 (on_ready 未到達) — I2b: 巻き戻し
            # (`_handle_pre_ready_failure`) は `commit()` より**前**に完了
            # させ、`commit()` には `slot_terminalize=False` を渡す。
            # `mark_terminal` は status ガードの無い無条件 UPDATE なので、
            # 先に revert した `reserved` を commit の無条件終端が潰す
            # のを防ぐ (I2 の主害)。
            should_retry = self._handle_pre_ready_failure(period_key, k)
            try:
                self._improve_loop.commit(
                    mission=mission, ctx=ctx, result=result,
                    now=self._clock.now(), slot_terminalize=False)
            except Exception as exc:
                _log.exception(
                    "improve pre-ready commit raised for slot %s/%s — "
                    "compensating", period_key, k)
                self._improve_loop.compensate_commit_failure(
                    ctx=ctx, now=self._clock.now(), exc=exc,
                    slot_terminalize=False)
                raise
            if not should_retry:
                return
            if self._stop_event.is_set():
                # advisor 指摘是正 (プラン10 束D round1、2026-08-28):
                # shutdown 中に再 spawn (新しい子プロセス + `worker_
                # startup_timeout_sec` の新しい待ち) を始めない —
                # `join()` docstring が引く `service.py:1203-1209` の
                # I-3 不変条件 (join budget は watchdog ceiling と同じ値
                # を共有する) に、retry loop 導入で 1 スレッドが最大 2 回
                # 分の spawn/待ちを行いうるようになったことが抵触する。
                # slot は既に `_handle_pre_ready_failure` が `reserved`
                # へ戻し終えている (次回起動時の reconcile/tick が拾う)。
                return
            now = self._clock.now()

    def _handle_pre_ready_failure(self, period_key: str, k: int) -> bool:
        """戻り値: `True` = slot を `reserved` へ戻した (呼び出し元は
        再 claim/再 spawn すること)。`False` = 再試行上限に達し `failed`
        へ収束した、または CAS 自体が失敗した (呼び出し元は retry しない)。

        C1 是正 (プラン10 束D round1、verified-codex-round1.md、
        2026-08-26 acceptance-task10-r2.md:304 の記録、2026-08-28):
        `revert_to_reserved`/`mark_slot_failed` の CAS 戻り値を消費する —
        従来は bool を無視していたため、CAS が rowcount=0 (slot が既に
        `claimed`/非終端でなくなっていた — 並行プロセスの回収等) でも
        呼び出し元は「意図どおり遷移した」ことにして retry/収束を続けて
        いた。CAS 失敗は「この slot はもう自分のものではない」ことの
        signal なので fail closed で `False` (retry しない) を返し、
        activity へ記録する — 呼び出し元 `_launch_slot` の `commit(...,
        slot_terminalize=False)` が mission/run だけを終端し、slot は
        (誰か他が既に扱った状態の) ままにする。"""
        conn = self._conn()
        owns = self._conn_for_test is None
        try:
            now = self._clock.now()
            row = conn.execute(
                "SELECT spawn_attempts FROM improve_wave_slots "
                "WHERE wave_period_key=? AND k=?", (period_key, k)).fetchone()
            attempts = row["spawn_attempts"]
            if attempts < _MAX_SPAWN_ATTEMPTS:
                ok = improve_waves.revert_to_reserved(
                    conn, period_key=period_key, k=k, now=now, commit=True)
                if not ok:
                    _log.warning(
                        "revert_to_reserved CAS failed for slot "
                        "(%s,%s) — slot no longer 'claimed', not retrying",
                        period_key, k)
                    if self._activity is not None:
                        self._activity.write(
                            Category.IMPROVE, "revert_to_reserved_cas_failed",
                            f"period_key={period_key} k={k}")
                    return False
                return True
            ok = improve_waves.mark_slot_failed(
                conn, period_key=period_key, k=k, now=now, commit=True)
            if not ok:
                _log.warning(
                    "mark_slot_failed CAS failed for slot (%s,%s) — slot "
                    "already terminal or missing", period_key, k)
                if self._activity is not None:
                    self._activity.write(
                        Category.IMPROVE, "mark_slot_failed_cas_failed",
                        f"period_key={period_key} k={k}")
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

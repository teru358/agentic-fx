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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agentic_fx import improve_notice_codes
from agentic_fx.activity import Category
from agentic_fx.core import improve_repeat as ir
from agentic_fx.core.scheduler import scheduled_period
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves

if TYPE_CHECKING:
    from agentic_fx.activity import ActivityLog
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock
    from agentic_fx.loops.improve_loop import ImproveLoop, ImprovePrepareFailed

_log = logging.getLogger("agentic_fx.improve_supervisor")

_MAX_SPAWN_ATTEMPTS = 2  # 初回 + 再試行 1 回 (設計書 §3.1 手順⑥)

#: 待ちの刻み (秒)。停止系と期限をこの間隔で見直す。
_POLL_SEC = 0.5


@dataclass(frozen=True)
class RepeatResult:
    stop_reason: str
    runs: int
    last_mission_id: int | None


class _ClaimHold:
    """gate の lock を保持している間だけ有効な握り。`release` は何度呼んでもよい。"""

    def __init__(self, cond: threading.Condition) -> None:
        self._cond = cond
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._cond.release()


class _NoticeEmitter:
    """改善 mission の開始・終了 event を出す唯一の場所。

    ops service の event store は使わず、`db_path` から専用の書込み接続と
    `EventStore` を作る (supervisor は ops service より先に作られる)。emit と終端確認は
    同じ lock の下で、同じ接続を使う (週次は並列に終端し得る)。`enabled` が偽なら接続も
    作らず何もしない。どの経路も `Exception` を呼び出し側へ出さない。技術ログへは固定語だけを
    書く。emit が通らなかったときは接続を捨て、次回に開き直す。取りこぼした finished は
    ops service の outbox 掃き出しが拾う。"""

    def __init__(self, *, db_path: Path, clock: "Clock", enabled: bool) -> None:
        self._db_path = db_path
        self._clock = clock
        self._enabled = enabled
        self._lock = threading.Lock()
        self._conn = None
        self._store = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def started(self, mission_id: int, trigger: str, repeat: dict | None = None) -> None:
        self._emit(mission_id, {
            "state": "started", "trigger": trigger, "result_code": None,
            "reason_code": None, "tool_calls": None, **(repeat or {})})

    def finished(self, mission_id: int,
                 exc: "ImprovePrepareFailed | None" = None,
                 repeat: dict | None = None) -> None:
        """終端 snapshot から `finished` を作る。mission が終端していなければ出さない。

        `exc` は prepare が Tx-0 の後で失敗し、補償で終端した場合の原因。"""
        if not self._enabled:
            return
        try:
            with self._lock:
                if self._closed:
                    return
                row = self._terminal_snapshot(mission_id)
        except Exception:  # noqa: BLE001 — 例外の文面は記録しない
            _log.warning("improve_notice_skipped reason=read_failed mission=%s",
                         mission_id)
            self._drop_connection()
            return
        if row is None:
            _log.warning("improve_notice_skipped reason=not_terminal mission=%s",
                         mission_id)
            return
        self._emit(mission_id, {
            "state": "finished", "trigger": row["trigger"],
            "result_code": row["result_code"], "reason_code": row["reason_code"],
            "tool_calls": row["tool_calls"], **(repeat or {})})

    def close(self) -> None:
        with self._lock:
            self._closed = True
            conn, self._conn, self._store = self._conn, None, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _drop_connection(self) -> None:
        with self._lock:
            conn, self._conn, self._store = self._conn, None, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_store(self):
        # lock を持って呼ぶ。接続は初回と、捨てた後にだけ開く。
        from agentic_fx.ops.events import EventStore
        if self._store is None:
            self._conn = db_mod.connect(self._db_path)
            self._store = EventStore(self._conn, wall_clock=self._clock.now)
        return self._store

    def _terminal_snapshot(self, mission_id: int):
        # lock を持って呼ぶ。emit と同じ接続で、呼ばれた時点の DB を見る。mission が
        # running でなく、run が終端し、snapshot がある、の 3 つが揃ったときだけ行が返る。
        self._ensure_store()
        return self._conn.execute(
            "SELECT n.trigger,n.result_code,n.reason_code,n.tool_calls "
            "FROM improve_run_notices n "
            "JOIN missions m ON m.id=n.mission_id AND m.status != 'running' "
            "JOIN improvement_runs r ON r.mission_id=n.mission_id "
            "AND r.finished_at IS NOT NULL "
            "WHERE n.mission_id=? LIMIT 1", (mission_id,)).fetchone()

    def _emit(self, mission_id: int, fields: dict) -> None:
        if not self._enabled:
            return
        from agentic_fx.ops.events import IMPROVE_CODE, EventSchemaError
        failed = False
        try:
            with self._lock:
                if self._closed:
                    return
                event_id = self._ensure_store().emit_best_effort(
                    IMPROVE_CODE, f"improve:{mission_id}", fields)
            failed = event_id is None
        except EventSchemaError:
            _log.warning("improve_notice_skipped reason=schema mission=%s", mission_id)
            return
        except Exception:  # noqa: BLE001
            _log.warning("improve_notice_skipped reason=emit_failed mission=%s",
                         mission_id)
            self._drop_connection()
            return
        if failed:
            _log.warning("improve_notice_emit_failed mission=%s state=%s",
                         mission_id, fields["state"])
            self._drop_connection()


class ImproveSupervisor:
    def __init__(self, *, capacity: int, root: Path, settings: "Settings",
                 clock: "Clock", db_path: Path,
                 stop_event: threading.Event,
                 activity: "ActivityLog | None" = None,
                 emit_events: bool = False) -> None:
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
        self._notices = _NoticeEmitter(
            db_path=db_path, clock=clock, enabled=emit_events)
        self._conn_for_test = None  # pytest シーム。本番は常に None。
        self._launch_lock = threading.Lock()
        self._active_threads: list[threading.Thread] = []
        # admission gate: 繰り返し側の mission 開始と週次 slot の claim を線形化する。
        # lock は admission の間だけ持ち、runner の実行中は持たない。
        self._gate = threading.Condition(threading.Lock())
        self._manual_active = False
        self._owned_slots: set[tuple[str, int]] = set()
        self._weekly_waiting = 0
        self._poll_sec = _POLL_SEC
        self._wait_fn: Callable[[threading.Event, float], object] = (
            lambda event, timeout: event.wait(timeout))

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
            period_key = scheduled_period(self._settings, now).period_key
        except Exception:
            _log.exception("ImproveSupervisor.tick: scheduled_period "
                           "failed for cadence=%r at=%r", s.improve, s.improve_at)
            if self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "improve_tick_schedule_error",
                    f"cadence={s.improve} at={s.improve_at!r}")
            raise
        conn = self._conn()
        owns = self._conn_for_test is None
        try:
            open_slots = self._capacity - self._running_slot_count(conn)
            m = min(self._settings.improve.parallel, max(open_slots, 0))
            if m == 0:
                return
            # wave / slot の作成と所有登録を同じ lock の中で行い、繰り返し側から
            # 「slot はあるが所有登録が無い」窓を見せない。
            with self._gate:
                created = improve_waves.create_wave_and_slots(
                    conn, period_key=period_key, now=now, expected=m,
                    commit=True)
                if not created:
                    return
                pending_ks = list(range(m))
                self._owned_slots.update((period_key, k) for k in pending_ks)
        finally:
            if owns:
                conn.close()
        # 起動に失敗した slot は fail-closed に収束させ、残りの slot は起動する。
        # 呼び出し元 (scheduler) が tick の異常を知れるよう、全 slot の処理後に
        # 最初の例外を送出し直す。
        first_error: Exception | None = None
        for k in pending_ks:
            try:
                self._spawn_slot_thread(period_key, k)
            except Exception as exc:  # noqa: BLE001
                _log.exception(
                    "improve slot thread start failed for %s/%s", period_key, k)
                self._converge_slot(period_key, k)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def submit_manual(self, *, on_prepared: "Callable[[int], None] | None" = None,
                      comment: str | None = None) -> int:
        """手動 one-shot。slot/wave 行を作らず M=1 で全バックログを担当
        させる (§8.1-該当、9.7 節「improve」コマンドから呼ばれる)。
        wave slot が無いため on_ready コールバックは不要 (mark_running する
        対象が無い) — `prepare(on_ready=)` を省略すると既定 `None` になる。
        admission gate は通らない。例外は従来どおり呼び出し元へ再送出する。"""
        return self._execute_mission(
            comment=comment, on_prepared=on_prepared, reraise=True).mission_id

    def _execute_mission(self, *, comment: str | None = None,
                         on_prepared: "Callable[[int], None] | None" = None,
                         reraise: bool = True, repeat: dict | None = None) -> ir.MissionOutcome:
        """1 mission の実行 (prepare -> run -> commit・補償)。

        `reraise` が真なら従来の手動実行と同じく例外を呼び出し元へ再送出する。
        偽なら例外は補償して失敗の outcome にする。outcome の status は補償後の
        `missions.status`。run の終了時に停止の Event が立っていれば `shutdown`
        (DB の mission は既存の補償終端のまま)。"""
        from agentic_fx.loops.improve_loop import ImprovePrepareFailed
        now = self._clock.now()
        kwargs: dict = {"slot_key": None, "now": now}
        if comment is not None:
            kwargs["oneoff_comment"] = comment
        if reraise:
            mission, ctx, runner = self._prepare(repeat=repeat, **kwargs)
        else:
            try:
                mission, ctx, runner = self._prepare_impl(kwargs, keep_failed=True, repeat=repeat)
            except ImprovePrepareFailed as exc:
                return self._outcome(exc.mission_id, None,
                                     self._stop_event.is_set(), read_db=True)
            except Exception:  # noqa: BLE001
                _log.exception("improve prepare raised for a repeat mission")
                return ir.MissionOutcome(
                    ir.STATUS_SHUTDOWN if self._stop_event.is_set() else "failed",
                    None, None)
        if on_prepared is not None:
            # 操作 API の job は mission id を走行中から見せる。通知の失敗で
            # mission を止めない。
            try:
                on_prepared(ctx.mission_id)
            except Exception:  # noqa: BLE001
                _log.exception("on_prepared callback failed")
        # runner.run() 自体が例外を投げる窓: 補償しないと mission / run が
        # 非終端のまま残るため、定期 slot と同じく補償する。
        # shutdown 系 (KeyboardInterrupt / SystemExit) では補償を新しく始めない。
        # 補償自身が失敗しても、元の例外を見せる (補償の失敗は log に残す)。
        try:
            result = runner.run(mission)
        except Exception:
            _log.exception(
                "improve runner.run raised for manual mission_id=%s — "
                "compensating", ctx.mission_id)
            stopping = self._stop_event.is_set()
            try:
                self._improve_loop.compensate_launch_failure(
                    ctx=ctx, now=self._clock.now())
            except Exception:
                _log.exception(
                    "launch compensation failed for manual mission_id=%s",
                    ctx.mission_id)
                self._record_compensation_failed(ctx.mission_id, "launch")
            self._notify_finished(ctx, repeat)
            if reraise:
                raise
            return self._outcome(ctx.mission_id, None, stopping, read_db=True)
        stopping = self._stop_event.is_set()
        try:
            self._improve_loop.commit(
                mission=mission, ctx=ctx, result=result, now=self._clock.now())
        except Exception as exc:
            _log.exception(
                "improve commit raised for manual mission_id=%s — "
                "compensating", ctx.mission_id)
            try:
                self._improve_loop.compensate_commit_failure(
                    ctx=ctx, now=self._clock.now(), exc=exc,
                    slot_terminalize=True, tool_calls=result.tool_calls)
            except Exception:
                _log.exception(
                    "commit compensation failed for manual mission_id=%s",
                    ctx.mission_id)
                self._record_compensation_failed(ctx.mission_id, "commit")
            self._notify_finished(ctx, repeat)
            if reraise:
                raise
            return self._outcome(ctx.mission_id, None, stopping, read_db=True)
        self._notify_finished(ctx, repeat)
        failure_class = getattr(result, "failure_class", None)
        if failure_class not in ir.FAILURE_CLASSES:
            failure_class = None
        return self._outcome(ctx.mission_id, failure_class, stopping,
                             read_db=not reraise,
                             runner_status=getattr(result, "status", None))

    def _outcome(self, mission_id, failure_class, stopping: bool, *,
                 read_db: bool, runner_status: str | None = None
                 ) -> ir.MissionOutcome:
        if stopping:
            return ir.MissionOutcome(ir.STATUS_SHUTDOWN, failure_class, mission_id)
        if read_db:
            status = self._read_mission_status(mission_id)
        else:
            status = "completed" if runner_status == "completed" else "failed"
        return ir.MissionOutcome(status, failure_class, mission_id)

    def _read_mission_status(self, mission_id) -> str:
        if mission_id is None:
            return "failed"
        try:
            conn = self._conn()
        except Exception:  # noqa: BLE001
            _log.exception("improve mission status read failed")
            return "failed"
        owns = self._conn_for_test is None
        try:
            row = conn.execute(
                "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
            return row["status"] if row is not None else "failed"
        except Exception:  # noqa: BLE001
            _log.exception("improve mission status read failed")
            return "failed"
        finally:
            if owns:
                conn.close()

    def _prepare(self, *, repeat: dict | None = None, **kwargs):
        """`prepare()` が Tx-0 後に失敗すると mission は補償で終端済みで、
        `ImprovePrepareFailed` が原因の例外を `__cause__` に持つ。呼び出し元
        (操作 API の例外分類など) が元の型で分岐できるよう、原因を再送出する。"""
        return self._prepare_impl(kwargs, keep_failed=False, repeat=repeat)

    def _prepare_impl(self, kwargs: dict, *, keep_failed: bool, repeat: dict | None = None):
        from agentic_fx.loops.improve_loop import ImprovePrepareFailed
        try:
            prepared = self._improve_loop.prepare(**kwargs)
        except ImprovePrepareFailed as exc:
            # 補償済みの mission は終端している。開始は出さず、終了だけ出す。
            self._notices.finished(exc.mission_id, exc, repeat=repeat)
            if keep_failed:
                raise
            if exc.__cause__ is not None:
                raise exc.__cause__
            raise
        trigger = (improve_notice_codes.TRIGGER_MANUAL
                   if kwargs.get("slot_key") is None
                   else improve_notice_codes.TRIGGER_SCHEDULED)
        if self._notices.enabled:
            self._notices.started(prepared[1].mission_id, trigger, repeat)
        return prepared

    def _notify_finished(self, ctx, repeat: dict | None = None) -> None:
        # 通知が無効なときは ctx に触れない。
        if self._notices.enabled:
            self._notices.finished(ctx.mission_id, repeat=repeat)

    def _record_compensation_failed(self, mission_id, phase: str) -> None:
        """補償の失敗を activity に残す。記録に失敗しても何も送出しない
        (呼び出し元は元の例外を上げ直す)。"""
        if self._activity is None:
            return
        try:
            self._activity.write(
                Category.IMPROVE, "compensation_failed",
                f"mission_id={mission_id} phase={phase}")
        except Exception:  # noqa: BLE001
            _log.exception("compensation_failed record failed")

    def shutdown(self) -> None:
        self._stop_event.set()
        # gate で待っている週次・繰り返しを起こす。lock を取れなくても止める
        # 動作は止めない (待ちは刻みごとに停止を見る)。
        if self._gate.acquire(timeout=1.0):
            try:
                self._gate.notify_all()
            finally:
                self._gate.release()

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
        deadline = (time.monotonic() + timeout
                    + self._settings.improve.accept_drain_sec)
        with self._launch_lock:
            threads = list(self._active_threads)
        for t in threads:
            remaining = deadline - time.monotonic()
            t.join(timeout=max(remaining, 0.0))
        with self._launch_lock:
            self._active_threads = [
                t for t in self._active_threads if t.is_alive()]
        self._notices.close()

    def is_alive(self) -> bool:
        with self._launch_lock:
            return any(t.is_alive() for t in self._active_threads)

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
        finally:
            self._converge_slot(period_key, k)

    def _converge_slot(self, period_key: str, k: int) -> None:
        """所有中の非終端 slot を fail-closed に `failed` へ落とし、所有登録を外して
        gate を起こす。終端済みの slot には何もしない (CAS)。"""
        try:
            conn = self._conn()
            owns = self._conn_for_test is None
            try:
                improve_waves.mark_slot_failed(
                    conn, period_key=period_key, k=k, now=self._clock.now(),
                    commit=True)
            finally:
                if owns:
                    conn.close()
        except Exception:  # noqa: BLE001
            _log.exception("improve slot convergence failed for %s/%s",
                           period_key, k)
        finally:
            with self._gate:
                self._owned_slots.discard((period_key, k))
                self._gate.notify_all()

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
        try:
            t.start()
        except BaseException:
            with self._launch_lock:
                self._active_threads = [
                    s for s in self._active_threads if s is not t]
            raise

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

            hold = self._weekly_enter()
            if hold is None:
                return
            # gate を待った後の時刻で claim する。
            now = self._clock.now()
            try:
                mission, ctx, runner = self._prepare(
                    slot_key=(period_key, k), now=now, on_ready=_on_ready,
                    on_claimed=hold.release)
            finally:
                hold.release()
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
                try:
                    self._improve_loop.compensate_launch_failure(
                        ctx=ctx, now=self._clock.now())
                finally:
                    self._notify_finished(ctx)
                raise
            if (reached_running or result.status == "completed"
                    or getattr(result, "refused", False)):
                # on_ready 到達後 (= running へ遷移済み) の失敗、または
                # 完走 — 通常の commit 終端へ渡す。retry しない (二重実行
                # を避ける、コメント上部の区別どおり)。隔離の判定の拒否も
                # ここで終端する (固定 reason なので起こし直しても同じ)。
                try:
                    self._improve_loop.commit(
                        mission=mission, ctx=ctx, result=result,
                        now=self._clock.now())
                except Exception as exc:
                    _log.exception(
                        "improve commit raised for slot %s/%s — compensating",
                        period_key, k)
                    try:
                        self._improve_loop.compensate_commit_failure(
                            ctx=ctx, now=self._clock.now(), exc=exc,
                            slot_terminalize=True, tool_calls=result.tool_calls)
                    finally:
                        self._notify_finished(ctx)
                    raise
                self._notify_finished(ctx)
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
                try:
                    self._improve_loop.compensate_commit_failure(
                        ctx=ctx, now=self._clock.now(), exc=exc,
                        slot_terminalize=False, tool_calls=result.tool_calls)
                finally:
                    self._notify_finished(ctx)
                raise
            self._notify_finished(ctx)
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
                # slot は `_handle_pre_ready_failure` が `reserved` へ戻した後、
                # スレッド終端の収束が `failed` にする (再起動時の回収も
                # `reserved` を `failed` にするので、どちらでも再実行されない)。
                return

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

    # ---- admission gate ------------------------------------------------

    def _weekly_enter(self) -> "_ClaimHold | None":
        """週次 slot の claim の前に呼ぶ。繰り返しの mission が走っている間は待ち、
        空いたら gate の lock を保持したまま戻る。呼び出し元は claim Tx の完了後
        (`on_claimed`) か失敗時に `release()` する。停止中は None (lock は放してある)。"""
        self._gate.acquire()
        hold = _ClaimHold(self._gate)
        try:
            self._weekly_waiting += 1
            try:
                while self._manual_active:
                    if self._stop_event.is_set():
                        break
                    self._gate.wait(self._poll_sec)
            finally:
                self._weekly_waiting -= 1
            if self._stop_event.is_set():
                hold.release()
                return None
            return hold
        except BaseException:
            hold.release()
            raise

    def _weekly_busy_locked(self, warned: set) -> bool:
        """gate の lock 内で呼ぶ。この process が所有する週次 slot に非終端のものが
        あるか。所有登録の無い非終端 slot は待たず、固定語の警告だけ出す。"""
        conn = self._conn()
        owns = self._conn_for_test is None
        try:
            rows = conn.execute(
                "SELECT wave_period_key, k FROM improve_wave_slots "
                "WHERE status IN ('reserved','claimed','running')").fetchall()
        finally:
            if owns:
                conn.close()
        busy = False
        for row in rows:
            key = (row["wave_period_key"], row["k"])
            if key in self._owned_slots:
                busy = True
            elif key not in warned:
                warned.add(key)
                _log.warning("improve_repeat_unowned_open_slot")
        return busy

    def _admit_manual(self, check: "Callable[[], str | None]") -> "str | None":
        """繰り返しの mission を始めてよくなるまで待つ。始めてよければ
        `manual_active` を立てて None を返す。停止の理由があればそれを返す。

        停止・期限・autopilot の確認 (`check`) は短い刻みで毎回行うが、gate の lock の
        外で呼ぶ (外部の排他 lock を待つことがあるため)。週次 slot の DB 確認は、
        gate の通知を受けたときと `WAIT_RECHECK_SEC` ごとだけにする。"""
        warned: set = set()
        busy = True
        need_db = True
        last_db = time.monotonic()
        while True:
            reason = check()
            if reason is not None:
                return reason
            with self._gate:
                if not self._manual_active and self._weekly_waiting == 0:
                    if need_db:
                        busy = self._weekly_busy_locked(warned)
                        last_db = time.monotonic()
                        need_db = False
                    if not busy:
                        self._manual_active = True
                        return None
                else:
                    need_db = True
                notified = self._gate.wait(self._poll_sec)
                if notified or time.monotonic() - last_db >= ir.WAIT_RECHECK_SEC:
                    need_db = True

    def _release_manual(self) -> None:
        with self._gate:
            self._manual_active = False
            self._gate.notify_all()

    # ---- 繰り返し --------------------------------------------------------

    def submit_repeat(self, *, duration_sec: "int | None", max_runs: "int | None",
                      comment_holder: "ir.CommentHolder | None",
                      on_progress: "Callable[[dict], None]",
                      cancel_event: threading.Event,
                      autopilot_check: "Callable[[], bool]",
                      monotonic: "Callable[[], float]" = time.monotonic,
                      deadline_mono: "float | None" = None,
                      deadline_at: "str | None" = None) -> RepeatResult:
        """mission を直列に繰り返す。期限は受理の時点で一度だけ決める:
        `deadline_mono` / `deadline_at` を渡せばそれを使い、渡さなければこの呼び出しの
        時点で単調時計と壁時計を同時に採って計算する。期限は新しい mission を始めて
        よいかの判定にだけ使う (1 回目は見ない)。"""
        if duration_sec is None and max_runs is None:
            raise ValueError("duration or max runs is required")
        limit = ir.MAX_RUNS if max_runs is None else ir.validate_max_runs(max_runs)
        if deadline_mono is None:
            span = ir.MAX_DURATION_SEC if duration_sec is None else duration_sec
            if (isinstance(span, bool) or not isinstance(span, (int, float))
                    or span <= 0 or span > ir.MAX_DURATION_SEC):
                raise ValueError("invalid duration")
            start_mono = monotonic()
            wall = self._clock.now()
            deadline_mono = start_mono + span
            deadline_at = self._iso_utc(wall + timedelta(seconds=span))
        elif deadline_at is None:
            wall = self._clock.now()
            deadline_at = self._iso_utc(
                wall + timedelta(seconds=deadline_mono - monotonic()))

        def progress(**patch) -> None:
            try:
                on_progress(patch)
            except Exception:  # noqa: BLE001
                _log.warning("improve_repeat_progress_failed")

        def stop_check(runs: int, consecutive: int,
                       autopilot: bool) -> "str | None":
            return ir.decide_next(
                runs=runs, max_runs=limit, mono_now=monotonic(),
                deadline_mono=deadline_mono, consecutive_failures=consecutive,
                last_resource_exhausted=False,
                stop_requested=cancel_event.is_set(),
                daemon_stopping=self._stop_event.is_set(), autopilot=autopilot)

        runs = 0
        consecutive = 0
        last_mission_id: "int | None" = None
        reason: "str | None" = None
        progress(phase="waiting_slot", runs=0, deadline_at=deadline_at)
        try:
            while True:
                reason = stop_check(runs, consecutive, bool(autopilot_check()))
                if reason is not None:
                    break
                if runs > 0:
                    progress(phase="waiting_slot")
                reason = self._admit_manual(
                    lambda: stop_check(runs, consecutive,
                                       bool(autopilot_check())))
                if reason is not None:
                    break
                try:
                    reason = stop_check(runs, consecutive, bool(autopilot_check()))
                    if reason is not None:
                        break
                    started = False

                    def on_prepared(mission_id: int) -> None:
                        nonlocal runs, last_mission_id, started
                        started = True
                        runs += 1
                        last_mission_id = mission_id
                        progress(phase="running", runs=runs, mission_id=mission_id)

                    comment = comment_holder.get() if comment_holder else None
                    context = {"repeat_run": runs + 1, "repeat_max_runs": limit,
                               "repeat_elapsed_sec": max(0, int(monotonic() - (deadline_mono - (
                                   ir.MAX_DURATION_SEC if duration_sec is None else duration_sec)))),
                               "repeat_duration_sec": (ir.MAX_DURATION_SEC if duration_sec is None
                                                       else duration_sec)}
                    outcome = self._execute_mission(
                        comment=comment, on_prepared=on_prepared, reraise=False,
                        repeat=context)
                finally:
                    self._release_manual()
                if not started:
                    runs += 1
                    if outcome.mission_id is not None:
                        last_mission_id = outcome.mission_id
                        progress(phase="running", runs=runs,
                                 mission_id=outcome.mission_id)
                    else:
                        progress(phase="running", runs=runs)
                if outcome.status == ir.STATUS_SHUTDOWN:
                    reason = ir.STOP_SHUTDOWN
                    break
                if outcome.status == "completed":
                    consecutive = 0
                    continue
                if (outcome.status in ("failed", "timeout")
                        and outcome.failure_class == ir.FAILURE_RESOURCE_EXHAUSTED):
                    reason = ir.STOP_RESOURCE_EXHAUSTED
                    break
                consecutive += 1
                reason = stop_check(runs, consecutive, False)
                if reason is not None:
                    break
                progress(phase="backoff")
                reason = self._backoff(
                    ir.BACKOFF_SEC, cancel_event, monotonic,
                    lambda: stop_check(runs, consecutive, False))
                if reason is not None:
                    break
        finally:
            if comment_holder is not None:
                comment_holder.clear()
            final_reason = reason if reason is not None else "error"
            if reason is not None:
                progress(phase="finished", stop_reason=reason)
            else:
                progress(phase="finished")
            if self._activity is not None:
                try:
                    self._activity.write(
                        Category.IMPROVE, "improve_repeat_finished",
                        f"reason={final_reason} runs={runs}")
                except Exception:  # noqa: BLE001
                    _log.warning("improve_repeat_activity_failed")
        return RepeatResult(reason, runs, last_mission_id)

    def _backoff(self, seconds: float, cancel_event: threading.Event,
                 monotonic: "Callable[[], float]",
                 check: "Callable[[], str | None]") -> "str | None":
        end = monotonic() + seconds
        while True:
            reason = check()
            if reason is not None:
                return reason
            remaining = end - monotonic()
            if remaining <= 0:
                return None
            self._wait_fn(cancel_event, min(remaining, self._poll_sec))

    @staticmethod
    def _iso_utc(moment: datetime) -> str:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()

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

"""ImproveSupervisor の wave/slot 状態機械 (設計書 §3.1、プラン §8.1-17〜19/26)。"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves
from agentic_fx.store import missions as missions_store


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "agentic.db"
    c = db_mod.connect(path)
    db_mod.init_db(c)
    return c


# Test helpers for 9.2 (and later sections)
class _FixedClock:
    """Fixed clock for testing."""
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _fake_settings(parallel: int) -> Any:
    """Minimal fake Settings for 9.2 wave creation testing.
    Real Settings consumer path (config.py ScheduleSettings) not yet in scope.
    """
    class _Cadence:
        improve = "weekly"
        improve_at = "Sat 03:00"

    class _Improve:
        def __init__(self, p: int) -> None:
            self.parallel = p

    class _FakeSettings:
        def __init__(self) -> None:
            self.schedule = _Cadence()
            self.improve = _Improve(parallel)
            self.display_timezone = "UTC"

    return _FakeSettings()


# Tests for 9.2: wave + slot Tx-0 creation + M=0

def test_wave_creation_writes_wave_and_m_slots_in_one_tx(conn):
    """M = min(parallel, 空き) 個の reserved slot が wave 行と**同じ commit**
    で現れる (part-way な状態が外部から観測できない — 単一 tx pin)。"""
    now = datetime(2026, 8, 22, 3, 0)
    ok = improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=2, commit=True)
    assert ok is True
    wave = conn.execute(
        "SELECT * FROM improve_waves WHERE period_key='2026-W34'").fetchone()
    assert wave["expected"] == 2
    slots = conn.execute(
        "SELECT k, status, mission_id FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' ORDER BY k").fetchall()
    assert [dict(s) for s in slots] == [
        {"k": 0, "status": "reserved", "mission_id": None},
        {"k": 1, "status": "reserved", "mission_id": None},
    ]


def test_wave_creation_is_idempotent_second_call_no_op(conn, monkeypatch):
    """`INSERT OR IGNORE` — 同じ period_key への 2 回目の呼び出しは
    rowcount=0 (起動権を得られない) で slot も増えない。ImproveSupervisor.tick
    が 2 回呼ばれても、再試行は発生しない (period は消費済み、
    _spawn_slot_thread も1回分のみ呼ばれる)。

    検収 B4 (2026-08-22): `now` は tz-aware UTC。naive datetime は
    `latest_scheduled_occurrence` がシステムローカル TZ で暗黙解釈して
    しまう (fail-open) ため B1 の修正で `ValueError` になった —
    naive では TZ=UTC 環境と開発機既定 (JST) で period key が食い違い、
    このテストがマシンのローカル TZ に依存していた。期待値
    '2026-W34' は B1 着地後の実装から実測導出
    (`display_timezone='UTC'` なので `now` の週がそのまま period key)。"""
    now = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
    sup = ImproveSupervisor(capacity=10, root=Path("/nonexistent"),
                             settings=_fake_settings(parallel=2),
                             clock=_FixedClock(now),
                             db_path=Path(conn.execute("PRAGMA database_list").fetchone()[2]),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    # Monkeypatch _spawn_slot_thread to record calls
    spawn_calls = []
    original_spawn = sup._spawn_slot_thread

    def recording_spawn(period_key, k):
        spawn_calls.append((period_key, k))

    monkeypatch.setattr(sup, "_spawn_slot_thread", recording_spawn)

    # First tick で wave + 2 slots を作成
    sup.tick(now)
    assert len(spawn_calls) == 2
    assert spawn_calls == [("2026-W34", 0), ("2026-W34", 1)]

    # Second tick で同じ period で再試行
    sup.tick(now)
    # spawn_calls には何も追加されない (period は既に消費済み)
    assert len(spawn_calls) == 2


def test_m_zero_creates_no_wave_row(conn, monkeypatch):
    """M=0 (空きスロット無し) は何も書かない — 次 tick で再試行できる
    (period 非消費)。ImproveSupervisor.tick が M を計算して 0 なら
    何もしないこと — create_wave_and_slots を呼ばず、queue も populate しない。"""
    call_count = []
    original_create = improve_waves.create_wave_and_slots
    def spy_create(*args, **kwargs):
        call_count.append(1)
        return original_create(*args, **kwargs)
    monkeypatch.setattr(improve_waves, "create_wave_and_slots", spy_create)

    sup = ImproveSupervisor(capacity=0, root=Path("/nonexistent"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)),
                             db_path=Path(conn.execute("PRAGMA database_list").fetchone()[2]),
                             stop_event=threading.Event())
    sup._conn_for_test = conn  # テストシーム
    sup.tick(datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc))
    # M3 mutation check: create_wave_and_slots should not be called when m=0
    assert len(call_count) == 0, f"create_wave_and_slots should not be called when m=0, but was called {len(call_count)} times"
    n = conn.execute("SELECT count(*) c FROM improve_waves").fetchone()["c"]
    assert n == 0


# Tests for 9.3: 3-way 起動プロトコル

class _RecordingFakeWorkerRunner:
    """WorkerRunner の代役 (裁定 R-D1、I1 是正で ok 検査順を本物に合わせて
    改訂 — プラン10 束D round1、verified-codex-round1.md、2026-08-28)。
    公開 API は `run(mission)` のみ (`on_ready` は本物と同じ公開属性)。
    `run()` 内部で「ready 受信 → **`ok` 検査 (false なら on_ready を
    呼ばず即 failed)** → on_ready 呼び出し → (例外なく戻れば) そのまま
    mission 実行を継続」という本物 `WorkerRunner.run()` の順序を模す —
    新規プロトコルフレーム (`go` 等) は追加しない (プラン 9.3 節
    「実装時改訂 R-D1」)。`ready_ok=False` は「子が Mission を一度も
    開始していない」(pre-ready 失敗) を表すため on_ready を呼ばない —
    「on_ready 到達後 (running 到達後) に失敗する」ケースは
    `body_status="failed"` (既定 `"completed"`) で表す。本物の
    ワイヤプロトコル (JSON フレーム・子プロセス) はここでは検証しない —
    骨格 Interfaces 節の `WorkerRunner(..., worker_profile="improve",
    run_context=ctx)` の構築タイミングと `on_ready` 完了後に実行が続く
    順序だけを外形的に確認する (実プロセスでの実測は
    `tests/runners/test_worker_runner.py` の R-D1/I1 統合テストが担う)。"""

    def __init__(self, events: list, *, on_ready=None, ready_ok: bool = True,
                body_status: str = "completed"):
        self._events = events
        self.on_ready = on_ready
        self._ready_ok = ready_ok
        self._body_status = body_status

    def run(self, mission):
        # I1: 本物と同じ順序 — `ok` 検査が on_ready より先。ok=False は
        # on_ready を呼ばずに即 failed (pre-ready 失敗)。
        if not self._ready_ok:
            return _FakeMissionResult(
                "failed", None, reason="child ready ok=false")
        ready_frame = {"type": "ready", "ok": self._ready_ok}
        if self.on_ready is not None:
            try:
                self.on_ready(ready_frame)
            except Exception as e:  # noqa: BLE001 — 本物の WorkerRunner.run()
                # と同じ規律: on_ready の例外は failed に正規化する
                # (呼び出し元へ伝播させない)。
                return _FakeMissionResult(
                    "failed", None, reason=f"on_ready failed: {e}")
        self._after_on_ready()
        self._events.append("run")
        return _FakeMissionResult(
            self._body_status, {} if self._body_status == "completed" else None)

    def _after_on_ready(self) -> None:
        """on_ready が例外なく戻った後、mission 実行を継続する直前のフック
        — サブクラスがここをオーバーライドして、この時点での状態 (DB 等)
        を検査できるようにする (本物の `WorkerRunner.run()` が on_ready
        完了後にそのまま実行を継続するタイミングと対応。新規フレームは
        無いため、送出ではなく「継続する直前」を切り出す)。"""


class _FakeMissionResult:
    """`runners.base.MissionResult` の代役 (属性アクセスのみ使う最小形)。"""

    def __init__(self, status, output, *, reason=None):
        self.status = status
        self.output = output
        self.reason = reason


class _FakeImproveLoop:
    """ImproveLoop (Task 10) の代役。Tx-0 の slot claim は `prepare()` の
    責務として fake でも忠実に再現する — `claim_slot` を実際に呼び
    `reserved→claimed` を tx で確定させる (fake が本物の `improve_waves`
    を呼ぶことで、Task 9 側は「claim は prepare の内側で起きる」という
    契約だけを検証すればよい)。"""

    def __init__(self, conn, worker_runner, *, mission_id=999):
        self._conn = conn
        self._worker_runner = worker_runner
        self._mission_id = mission_id
        self.committed: list[tuple] = []
        # 裁定 D1: improve_wave_slots.mission_id に FK が付いたため、claim
        # 対象の mission_id は実在する missions 行でなければならない。
        # テストは特定の literal id (999/111/222 等) を assert しているため、
        # id を明示指定した INSERT で実在させる (無ければ作る)。
        conn.execute(
            "INSERT OR IGNORE INTO missions "
            "(id, loop, runner, model, status, started_at) "
            "VALUES (?, 'improve', 'local', 'm', 'running', ?)",
            (mission_id, "2026-08-22T03:00:00"))
        conn.commit()

    def prepare(self, *, slot_key, now, on_ready=None):
        period_key, k = slot_key
        claimed = improve_waves.claim_slot(
            self._conn, period_key=period_key, k=k, mission_id=self._mission_id,
            now=now, commit=True)
        if not claimed:
            raise RuntimeError(
                f"slot claim failed for {slot_key!r} — "
                "already claimed by a concurrent process")
        # R-D1: 本物の ImproveLoop.prepare() は on_ready を WorkerRunner の
        # 構築に渡す (Task 10 の責務) — fake でも同じ契約を模し、
        # _launch_slot が渡した on_ready を fake runner へ配線する。
        if on_ready is not None:
            self._worker_runner.on_ready = on_ready
        return f"mission-{self._mission_id}", f"ctx-{self._mission_id}", \
            self._worker_runner

    def commit(self, *, mission, ctx, result, now, slot_terminalize=True):
        # I2b 是正 (2026-08-28): 実 `ImproveLoop.commit` は
        # `slot_terminalize=False` を受け取る (`_launch_slot` が pre-ready
        # 失敗の再試行時に渡す)。fake は記録するだけで DB には触れない
        # (この fake を使うテストは slot 状態を `improve_waves` 直呼びで
        # 検証するため、`slot_terminalize` の実効果は
        # `tests/loops/test_improve_loop_finalize.py` 側で pin 済み)。
        self.committed.append((mission, ctx, result, slot_terminalize))


def test_three_way_launch_order_prepare_then_ready_then_running_commit_then_run(
        conn, monkeypatch):
    """prepare (Tx-0, claim 含む) → runner.run() 内部で ready 受信 →
    on_ready (= running commit) → run → commit()、の順序を固定する
    (裁定 R-D1: spawn/send_go/run_to_completion の 3 相 API は撤去。新規
    プロトコルフレームは追加しない)。"""
    events: list[str] = []
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)

    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn
    fake_loop = _FakeImproveLoop(
        conn, _RecordingFakeWorkerRunner(events))
    sup._improve_loop = fake_loop
    sup._launch_slot("2026-W34", 0)

    row = conn.execute(
        "SELECT status, mission_id FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "running"
    assert row["mission_id"] == 999
    assert events == ["run"]
    assert len(fake_loop.committed) == 1


def test_mission_body_not_continued_before_running_commit(conn):
    """running への commit が完了する前に mission 本体の実行を継続しない
    ことを、`_after_on_ready` の内部で slot 状態を読み返して確認する
    fake WorkerRunner 経由で確認する。commit 前に継続が記録されたら
    fail (裁定 R-D1: 新規フレームは無いので、継続そのものが同期点)。"""
    events: list[str] = []
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)

    class _OrderCheckingRunner(_RecordingFakeWorkerRunner):
        def _after_on_ready(self):
            row = conn.execute(
                "SELECT status FROM improve_wave_slots "
                "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
            assert row["status"] == "running", (
                "mission body continued before the running-commit was "
                "visible")
            super()._after_on_ready()

    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn
    sup._improve_loop = _FakeImproveLoop(conn, _OrderCheckingRunner(events))
    sup._launch_slot("2026-W34", 0)


def test_mark_running_cas_failure_does_not_authorize_the_child(conn):
    """C1 是正 (プラン10 束D round1、verified-codex-round1.md、
    2026-08-28): `mark_running` の CAS が rowcount=0 (slot が既に
    `claimed` でなくなっていた — 並行プロセスの起動時 reconcile 等) でも
    `_on_ready` が無条件に `reached_running = True` としていたため、
    親が `running` を durable commit できていないのに子の Mission 本体が
    実行されていた (設計 §3.1③ authorization 順序の破れ)。fake runner が
    `on_ready` 呼び出しの**直前**に slot を他経路で `failed` へ終端させ、
    CAS を確実に失敗させる (probe_c1_i2_i3.py::probe_c1 と同形)。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=now, expected=1, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    events: list[str] = []

    class _CasRacingRunner(_RecordingFakeWorkerRunner):
        def run(self, mission):
            # 親が mark_running を打つ直前に slot を他経路で終端したことに
            # する (= CAS `AND status='claimed'` が当たらず rowcount=0)。
            conn.execute(
                "UPDATE improve_wave_slots SET status='failed' "
                "WHERE wave_period_key='2026-W34' AND k=0")
            conn.commit()
            return super().run(mission)

    fake_loop = _FakeImproveLoop(conn, _CasRacingRunner(events), mission_id=333)
    sup._improve_loop = fake_loop
    sup._launch_slot("2026-W34", 0)

    # (a) on_ready の例外で fake runner が failed へ正規化し、mission 本体
    # (events への "run" 追記) は一度も実行されない。
    assert events == []
    # (c) `_handle_pre_ready_failure` は CAS 失敗 (slot は既に 'failed'
    # で 'claimed' ではない) を検出して retry しない — commit() は 1 回
    # だけ呼ばれる (retry すると 2 回目の `prepare()`/`claim_slot` が
    # 'failed' slot に対して失敗し、別の例外に化けていたはず)。
    assert len(fake_loop.committed) == 1
    assert fake_loop.committed[0][2].status == "failed"
    row = conn.execute(
        "SELECT status FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "failed"


def test_handle_pre_ready_failure_revert_cas_failure_does_not_retry(conn):
    """C1 是正の付随: `_handle_pre_ready_failure` 自身が
    `revert_to_reserved`/`mark_slot_failed` の CAS 戻り値を消費すること
    を単体で pin する (`acceptance-task10-r2.md:304` の記録 —
    `_handle_pre_ready_failure:198-202` の bool 無視)。slot が既に
    (他経路で) `failed` に終端済みのとき、`revert_to_reserved` の CAS
    (`WHERE status='claimed'`) は当たらない — bool を無視すると
    「reserved に戻した」と誤認して呼び出し元に retry (=再 claim) を
    促してしまう。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=now, expected=1, commit=True)
    conn.execute(
        "INSERT INTO missions (id, loop, runner, model, status, started_at) "
        "VALUES (444, 'improve', 'local', 'm', 'running', ?)",
        (now.isoformat(),))
    assert improve_waves.claim_slot(
        conn, period_key="2026-W34", k=0, mission_id=444, now=now, commit=True)
    # spawn_attempts=1 (< _MAX_SPAWN_ATTEMPTS) の状態で、slot を他経路で
    # 'failed' へ終端させる (revert_to_reserved の CAS を確実に外す)。
    conn.execute(
        "UPDATE improve_wave_slots SET status='failed' "
        "WHERE wave_period_key='2026-W34' AND k=0")
    conn.commit()

    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    should_retry = sup._handle_pre_ready_failure("2026-W34", 0)

    assert should_retry is False, (
        "revert_to_reserved の CAS が失敗しているのに retry を促している")
    row = conn.execute(
        "SELECT status FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "failed", "CAS 失敗時に他経路の状態を上書きしない"


def test_on_ready_exception_reverts_to_reserved_then_failed(conn, monkeypatch):
    """I2b 是正 (プラン10 束D round1、ユーザー裁定 D①、2026-08-28、逸脱
    申告): on_ready (= `_launch_slot` が組む mark_running クロージャ) が
    例外を投げたら、pre-ready 失敗と同じ経路 (spawn_attempts に応じて
    reserved→failed) を辿る。run は一度も記録されない (fake
    `WorkerRunner.run()` は本物と同じく on_ready の例外を実行継続の
    **前**で捕捉し failed に正規化する)。

    **書き換え理由**: I2(b) の是正で `_launch_slot` が同一プロセス内で
    pre-ready 失敗を再試行するようになった (設計 §3.1⑥/§8.1-19)。旧版は
    「2 つの独立プロセス」を模して `_launch_slot` を**2 回**呼んでいたが、
    新実装では**1 回**の呼び出しが内部で 2 回 (初回+再試行1回) 試みる —
    2 回呼ぶと 4 attempt 分進んでしまい old 版の期待値と矛盾する。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=now, expected=1, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    def _boom(*a, **k):
        raise RuntimeError("mark_running boom")
    monkeypatch.setattr(improve_waves, "mark_running", _boom)

    events: list[str] = []
    fake_loop = _FakeImproveLoop(
        conn, _RecordingFakeWorkerRunner(events), mission_id=111)
    sup._improve_loop = fake_loop
    sup._launch_slot("2026-W34", 0)  # 1 回の呼び出しで初回+再試行1回

    row = conn.execute(
        "SELECT status, spawn_attempts FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "failed"
    assert row["spawn_attempts"] == 2
    assert events == []
    # commit() は各 attempt ごとに (2 回) 呼ばれる — pre-ready 失敗でも
    # 早期 return せず必ず commit する契約 (mission/run 終端) を維持し
    # つつ、両方とも slot_terminalize=False (呼び出し元が既に revert/
    # mark_slot_failed で slot を扱い終えている) で呼ばれる。
    assert len(fake_loop.committed) == 2
    assert [c[3] for c in fake_loop.committed] == [False, False]


def test_failure_after_running_does_not_retry_the_slot(conn):
    """D03 是正 (段0 致命1): `not reached_running` の判別が外れると、
    on_ready 到達後 (= slot が `running` へ遷移済み) の失敗まで
    `_handle_pre_ready_failure` の retry 経路に乗ってしまい、二重実行に
    つながる。コード自身のコメント (改訂履歴の直上) が「実行が始まった
    後の失敗を retry してしまうと二重実行になるため区別する」と明記する
    防御を pin する。

    1 回目: on_ready へ一度も到達せず failed (pre-ready 失敗) —
    正規の revert_to_reserved で `reserved` に戻る (spawn_attempts=1)。
    2 回目: on_ready へ到達してから ("running" へ遷移済み) failed を返す
    — `not reached_running` が効いていれば `_handle_pre_ready_failure` は
    呼ばれず、slot は `running` のまま (commit() 側の通常終端に委ねる)。
    変異 (`not reached_running and ...` → `...`) が入ると、2 回目の
    attempts=2 (>= _MAX_SPAWN_ATTEMPTS) で `mark_slot_failed` が呼ばれ、
    その CAS は `running` も受理するため実行中の slot が
    `failed` + `mission_id=NULL` に落ちる (§6.7 — 1 attempt 目だけを見る
    形だと `revert_to_reserved` の CAS が `claimed` 限定で no-op になり
    観測結果が無変異時と一致してしまうため、2 attempt 目まで踏む)。

    I2b 是正の逸脱申告 (2026-08-28): 同一プロセス内 retry が実装された
    ため、旧版の「2 回の独立呼び出し」は 1 回の `_launch_slot` 呼び出しに
    統合する。1 attempt 目 (pre-ready 失敗) と 2 attempt 目 (on_ready 到達
    後の失敗) とで挙動を変える必要があるため、呼び出し回数で分岐する
    stateful runner を使う (`_FakeImproveLoop` は同一 `mission_id`/
    `worker_runner` を両 attempt で使い回す — 記帳の commit() は no-op な
    ので、実際の mission 行整合はここでは問わない。実 `finish_improve_
    mission` を通す検証は `test_pre_ready_failure_keeps_slot_reserved_
    through_real_commit` が別途担う)。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=now, expected=1, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    events: list[str] = []

    class _PreReadyThenPostReadyFailsRunner:
        """1 attempt 目: on_ready へ一度も到達せず failed (pre-ready 失敗)。
        2 attempt 目: on_ready へ到達 (`ready_ok=True`) して running へ
        遷移した**後**に mission 本体が failed で終わる (I1 是正後の
        `_RecordingFakeWorkerRunner` は `ready_ok=False` で on_ready を
        呼ばなくなったため、post-ready 失敗は `body_status="failed"` で
        表す — on_ready 自体は呼ばれ reached_running=True になる点が
        pre-ready 失敗との違い)。"""
        on_ready = None

        def __init__(self):
            self._calls = 0

        def run(self, mission):
            self._calls += 1
            if self._calls == 1:
                return _FakeMissionResult(
                    "failed", None, reason="pre-ready failure")
            delegate = _RecordingFakeWorkerRunner(
                events, on_ready=self.on_ready, ready_ok=True,
                body_status="failed")
            return delegate.run(mission)

    sup._improve_loop = _FakeImproveLoop(
        conn, _PreReadyThenPostReadyFailsRunner(), mission_id=111)
    sup._launch_slot("2026-W34", 0)  # 1 回の呼び出しで両 attempt を進める

    row = conn.execute(
        "SELECT status, mission_id, spawn_attempts FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "running", (
        "on_ready 到達後 (running へ遷移済み) の失敗が retry 経路 "
        "(_handle_pre_ready_failure) に乗り、実行中の slot が終端されて "
        "しまっている — 二重実行の危険がある")
    assert row["mission_id"] == 111
    assert row["spawn_attempts"] == 2


# Tests for 9.4: pre-ready 失敗

def test_pre_ready_failure_reverts_to_reserved_with_mission_id_null(conn):
    """I2b 是正の逸脱申告 (2026-08-28): 「1 回の pre-ready 失敗は
    `reserved` に戻る」という**単一 attempt の遷移**を pin する。旧版は
    `_launch_slot` を 1 回呼べば 1 attempt だけ進む前提だったが、
    I2(b) 是正で `_launch_slot` は同一呼び出し内で最大 2 attempt
    (初回+再試行1回) を進めるようになったため、常に失敗する fake
    runner で `_launch_slot` を 1 回呼ぶと最終的に `failed`/
    `spawn_attempts=2` まで進んでしまい、この pin (1 attempt 目だけの
    遷移) を単独では観測できなくなった。単一 attempt の遷移そのものは
    `ImproveSupervisor._handle_pre_ready_failure` (retry loop から
    切り出された、CAS 呼び分けの本体) を直接呼んで検証する — `_launch_slot`
    経由の複数 attempt 統合は `test_on_ready_exception_reverts_to_
    reserved_then_failed`/`test_pre_ready_failure_second_attempt_goes_
    to_failed` (単一呼び出しで両 attempt を進める形に書き換え済み) が
    担う。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)
    conn.execute(
        "INSERT INTO missions (id, loop, runner, model, status, started_at) "
        "VALUES (111, 'improve', 'local', 'm', 'running', ?)",
        (now.isoformat(),))
    assert improve_waves.claim_slot(
        conn, period_key="2026-W34", k=0, mission_id=111, now=now, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    should_retry = sup._handle_pre_ready_failure("2026-W34", 0)

    row = conn.execute(
        "SELECT status, mission_id, spawn_attempts FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "reserved"
    assert row["mission_id"] is None
    assert row["spawn_attempts"] == 1
    assert should_retry is True, (
        "spawn_attempts=1 < _MAX_SPAWN_ATTEMPTS なので retry すべき")


def test_pre_ready_failure_second_attempt_goes_to_failed(conn):
    """spawn_attempts が 2 に達したら (初回 + 再試行 1 回) failed へ収束する
    (I2b 是正の逸脱申告: 単一 `_launch_slot` 呼び出しで両 attempt を
    進める形へ書き換え — 上の pin と同じ理由)。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    class _PreReadyFailsRunner:
        on_ready = None

        def run(self, mission):
            return _FakeMissionResult("failed", None, reason="pre-ready failure")

    sup._improve_loop = _FakeImproveLoop(conn, _PreReadyFailsRunner(),
                                         mission_id=111)
    sup._launch_slot("2026-W34", 0)  # 1 回の呼び出しで初回+再試行1回 → failed

    row = conn.execute(
        "SELECT status, mission_id, spawn_attempts FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "failed"
    assert row["spawn_attempts"] == 2


def test_pre_ready_failure_also_covers_ready_timeout_before_running_commit(conn):
    """`ready` 受信前の timeout も pre-ready 失敗と同じ経路 (spawn 成功後、
    ready が来ない/timeout するケース) — `on_ready` を一度も呼ばずに
    failed を返す fake で模す。単一 attempt の遷移 pin (上と同じ理由で
    `_handle_pre_ready_failure` を直接呼ぶ形へ書き換え、逸脱申告)。"""
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)
    conn.execute(
        "INSERT INTO missions (id, loop, runner, model, status, started_at) "
        "VALUES (111, 'improve', 'local', 'm', 'running', ?)",
        (now.isoformat(),))
    assert improve_waves.claim_slot(
        conn, period_key="2026-W34", k=0, mission_id=111, now=now, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    sup._handle_pre_ready_failure("2026-W34", 0)
    row = conn.execute(
        "SELECT status, mission_id FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "reserved"
    assert row["mission_id"] is None


def test_pre_ready_failure_keeps_slot_reserved_through_real_commit(conn):
    """I2b 是正の核心 pin (プラン10 束D round1、ユーザー裁定 D①、
    2026-08-28): `_FakeImproveLoop.commit` は no-op なので上記のテスト
    群は「commit() が slot を潰さないか」を検証できない。ここでは
    `commit()` が実 `missions_store.finish_improve_mission` を呼ぶ fake
    (verified-codex-round1.md I2 probe と同形) を使い、`_launch_slot` の
    retry loop を通しで実行して、1 attempt 目の revert が commit の
    無条件終端で潰されないこと、2 attempt 目で最終的に failed へ収束
    すること、mission が **2 件** (attempt ごとに新規) 作られ両方とも
    failed で終端することを実測する。"""
    from agentic_fx.store import improve_runs as improve_runs_store

    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=now, expected=1, commit=True)
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    prepared_mission_ids: list[int] = []

    class _PreReadyFailsRunner:
        on_ready = None

        def run(self, mission):
            return _FakeMissionResult("failed", None, reason="pre-ready failure")

    class _RealCommitFakeImproveLoop:
        """実 `missions_store`/`improve_runs_store`/`improve_waves` を
        通して Tx-0 を書き、`commit()` は実 `finish_improve_mission` を
        呼ぶ (probe_c1_i2_i3.py::probe_i2 と同形)。"""

        def __init__(self):
            self.committed: list[tuple] = []

        def prepare(self, *, slot_key, now, on_ready=None):
            period_key, k = slot_key
            mid = missions_store.start(
                conn, "improve", "local", "m", now=now, commit=False)
            rid = improve_runs_store.start(
                conn, backlog_id=None, mission_id=mid, now=now, commit=False)
            claimed = improve_waves.claim_slot(
                conn, period_key=period_key, k=k, mission_id=mid, now=now,
                commit=False)
            assert claimed
            conn.commit()
            prepared_mission_ids.append(mid)
            runner = _PreReadyFailsRunner()
            runner.on_ready = on_ready
            return (mid, rid, slot_key), None, runner

        def commit(self, *, mission, ctx, result, now, slot_terminalize=True):
            mission_id, run_id, slot_key = mission
            self.committed.append((mission_id, slot_terminalize))
            conn.execute("BEGIN IMMEDIATE")
            missions_store.finish_improve_mission(
                conn, mission_id=mission_id, run_id=run_id,
                slot_key=slot_key if slot_terminalize else None,
                mission_status="failed", run_result=None, now=now,
                backlog_transition=None, commit=False)
            conn.commit()

    fake_loop = _RealCommitFakeImproveLoop()
    sup._improve_loop = fake_loop

    # probe_c1_i2_i3.py::probe_i2 と同じ技法: improve_wave_slots に当たる
    # UPDATE を順序どおり採取し、「1 attempt 目の revert (→reserved) の
    # 直後に commit の無条件終端 (→failed) が来ていない」ことを直接見る
    # (最終状態だけでは検出できない — 上記コメント参照)。
    trace: list[str] = []
    conn.set_trace_callback(
        lambda s: trace.append(" ".join(s.split()))
        if "improve_wave_slots" in s and "SET" in s else None)
    sup._launch_slot("2026-W34", 0)
    conn.set_trace_callback(None)

    status_writes = [s for s in trace if "status=" in s]
    assert len(status_writes) >= 3, status_writes
    # 1 attempt 目: claimed → reserved (revert)。この直後に failed へ
    # 落ちていたら I2b の欠陥が再現している (commit の無条件終端が
    # revert を潰した)。
    assert "status='reserved'" in status_writes[1], status_writes
    assert "status='failed'" not in status_writes[2], (
        "revert_to_reserved の直後の1手が failed — commit() の無条件終端 "
        f"が revert を潰している: {status_writes}")

    slot = conn.execute(
        "SELECT status, mission_id, spawn_attempts FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert slot["status"] == "failed"
    assert slot["spawn_attempts"] == 2
    # 主張の核心: I2b が無ければ 1 attempt 目の revert_to_reserved 直後の
    # commit (無条件 finish_improve_mission) が `reserved` を `failed` へ
    # 潰す — その場合でも最終状態は同じ `failed` になるため、最終状態
    # だけでは変異を検出できない (verified-codex-round1.md I2「採っては
    # いけない形」)。killer は「2 件の**別々の** mission が作られ、両方
    # とも failed で終端している」こと — I2b が無いと 1 attempt 目の
    # commit で `finish_improve_mission(slot_key=<real slot_key>)` が
    # 呼ばれても mission 自体は同じく failed になるため、ここでは
    # **`_handle_pre_ready_failure` の CAS が両 attempt で機能したか**
    # (spawn_attempts の刻み) と **prepare が 2 回呼ばれたか** を併せて
    # pin する。
    assert len(prepared_mission_ids) == 2
    assert len(set(prepared_mission_ids)) == 2, "2 attempt は別々の mission"
    for mid in prepared_mission_ids:
        m = conn.execute(
            "SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
        assert m["status"] == "failed"
    assert [c[1] for c in fake_loop.committed] == [False, False]


# Tests for 9.5: N-slot 構成・接続所有・shutdown/join

def test_n4_concurrent_slots_no_connection_sharing_no_mixup(tmp_path):
    """N=4 同時 slot が別々の write 接続を持ち、混線 (他 slot の行を触る)
    が無いことを実測する。各 slot は自分の period_key/k だけを更新する
    fake `ImproveLoop`/worker で 4 本同時実行し、結果の整合を確認する。
    `_improve_loop.prepare` (Tx-0 の claim) が各スレッド自身の DB 接続で
    行われることを、fake `ImproveLoop` が `db_mod.connect(db_path)` を
    スレッドごとに開くことで再現する (統合裁定 R-i2 — claim は
    `ImproveLoop.prepare` 側の責務)。"""
    db_path = tmp_path / "agentic.db"
    conn0 = db_mod.connect(db_path)
    db_mod.init_db(conn0)
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn0, period_key="2026-W34", now=now, expected=4, commit=True)
    # 裁定 D1: mission_id=100+k (k=0..3) が claim 対象。FK のため事前に
    # 実在させる (id を明示指定した INSERT)。
    for k in range(4):
        conn0.execute(
            "INSERT INTO missions (id, loop, runner, model, status, started_at) "
            "VALUES (?, 'improve', 'local', 'm', 'running', ?)",
            (100 + k, now.isoformat()))
    conn0.commit()
    conn0.close()

    sup = ImproveSupervisor(capacity=4, root=tmp_path,
                             settings=_fake_settings(parallel=4),
                             clock=_FixedClock(now), db_path=db_path,
                             stop_event=threading.Event())

    class _SlowRunner:
        on_ready = None

        def run(self, mission):
            time.sleep(0.05)
            if self.on_ready is not None:
                self.on_ready({"type": "ready", "ok": True})
            time.sleep(0.05)
            return _FakeMissionResult("completed", {})

    class _PerThreadFakeImproveLoop:
        """各スレッドが自分専用の write 接続で `claim_slot`/`commit` を
        行う fake (本番の `ImproveLoop` の接続所有パターンを模す)。"""

        def prepare(self, *, slot_key, now, on_ready=None):
            period_key, k = slot_key
            conn = db_mod.connect(db_path)
            try:
                mission_id = 100 + k
                claimed = improve_waves.claim_slot(
                    conn, period_key=period_key, k=k, mission_id=mission_id,
                    now=now, commit=True)
                assert claimed, f"slot {slot_key!r} already claimed"
            finally:
                conn.close()
            runner = _SlowRunner()
            runner.on_ready = on_ready
            return mission_id, f"ctx-{mission_id}", runner

        def commit(self, *, mission, ctx, result, now):
            conn = db_mod.connect(db_path)
            try:
                conn.execute(
                    "UPDATE improve_wave_slots SET status='done' "
                    "WHERE mission_id=?", (mission,))
                conn.commit()
            finally:
                conn.close()

    sup._improve_loop = _PerThreadFakeImproveLoop()

    threads = [threading.Thread(target=sup._launch_slot,
                                args=("2026-W34", k))
              for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    conn1 = db_mod.connect(db_path)
    rows = conn1.execute(
        "SELECT k, status FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' ORDER BY k").fetchall()
    assert [r["status"] for r in rows] == ["done", "done", "done", "done"]
    conn1.close()


def test_trade_writer_not_blocked_during_improve_gate_tx(tmp_path):
    """改善レーンの長い作業 (ここでは意図的に BEGIN IMMEDIATE を保持する
    fake) の**外側**で、取引レーンの writer が busy_timeout (5000ms) 内に
    通ることを確認する — Tx がロング処理を跨がないことの負荷実測代理。"""
    db_path = tmp_path / "agentic.db"
    conn0 = db_mod.connect(db_path)
    db_mod.init_db(conn0)
    conn0.close()

    improve_conn = db_mod.connect(db_path)
    trade_conn = db_mod.connect(db_path)

    # 改善レーンの「短い tx」を模す: 開いてすぐ commit する (transaction
    # 外の長時間処理と対比するため、ここでは意図的に「短い」ことを
    # assert する — 長時間 held のケースは Task 10 の Tx-1/Tx-2 の実装
    # レビューで別途 fault injection する)。
    improve_conn.execute("BEGIN IMMEDIATE")
    improve_conn.execute(
        "UPDATE improve_wave_slots SET status='running' WHERE 1=0")
    improve_conn.commit()

    started = time.monotonic()
    trade_conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('trade','local','x','running',?)", (datetime.now().isoformat(),))
    trade_conn.commit()
    elapsed = time.monotonic() - started
    assert elapsed < 1.0  # busy_timeout (5000ms) 内、実務上は瞬時

    improve_conn.close()
    trade_conn.close()


@pytest.mark.parametrize("occupied_status", ["claimed", "running"])
def test_wave_creation_respects_running_slot_count(
        tmp_path, monkeypatch, occupied_status):
    """M = min(parallel, open_slots) の capacity 制限テスト。
    capacity=2, parallel=4, 1 slot が occupied (`claimed`/`running`) →
    1 slot だけ作成される。

    D02 是正 (段0 準致命): `claimed` は Tx-0 完了・ready 未到達 =
    既に子プロセスを起こしにいっている slot。数え落とすと `open_slots`
    が過大になり capacity を超えて wave/slot を作り CLI 子プロセスを
    余分に起動する。以前は `running` のみを踏んでおり、`_running_slot_
    count` の `IN ('claimed','running')` → `IN ('running')` という
    「占有中の数え落とし」変異を検出できていなかった (§6.11)。占有状態を
    固定リテラル `["claimed", "running"]` で parametrize し、両方を
    踏ませる — 本番の集合を parametrize に渡す形は 2026-08-23 束 C の
    教訓 (変異が自分のテストケースを静かに消す) により採らない。"""
    db_path = tmp_path / "agentic.db"
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)
    now = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)

    # Create an occupied slot from a previous period (not the current tick's period)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W32", now=now, expected=1, commit=True)
    mid = missions_store.start(conn, "improve", "local", "m", now=now)
    conn.execute(
        "UPDATE improve_wave_slots SET status=?, mission_id=? "
        "WHERE wave_period_key='2026-W32' AND k=0", (occupied_status, mid))
    conn.commit()

    # Now try to create new wave with capacity=2, parallel=4
    sup = ImproveSupervisor(capacity=2, root=tmp_path,
                             settings=_fake_settings(parallel=4),
                             clock=_FixedClock(now),
                             db_path=db_path,
                             stop_event=threading.Event())
    sup._conn_for_test = conn

    # Monkeypatch _spawn_slot_thread to avoid daemon thread errors
    spawn_calls = []
    monkeypatch.setattr(sup, "_spawn_slot_thread",
                       lambda period_key, k: spawn_calls.append((period_key, k)))

    sup.tick(now)

    # Check that only 1 new slot was spawned (capacity-running_count = 2-1 = 1)
    assert len(spawn_calls) == 1
    assert spawn_calls[0][1] == 0  # slot k=0

    # Check DB: 1 running from prev period + 1 new = 2 total
    rows = conn.execute(
        "SELECT count(*) c FROM improve_wave_slots").fetchone()
    assert rows["c"] == 2

    conn.close()


# Tests for B5 (検収 2026-08-22): join budget は全体で 1 つの deadline

def test_join_budget_is_shared_not_multiplied_by_thread_count(tmp_path):
    """`ImproveSupervisor.join(timeout)` は N 個のスレッドが**全部**
    timeout しても、消費時間が `timeout × N` に膨らんではならない —
    全体で 1 つの deadline (`monotonic() + timeout`) を共有し、残余を
    各スレッドへ配る形であること。N=3、timeout=0.2s で検証: 修正前の
    実装 (各スレッドへ timeout をまるごと渡す) だと最悪 0.6s 消費するが、
    修正後は 0.2s 前後で返ること。"""
    sup = ImproveSupervisor(capacity=3, root=tmp_path,
                             settings=_fake_settings(parallel=3),
                             clock=_FixedClock(datetime(2026, 8, 22, 3, 0,
                                                        tzinfo=timezone.utc)),
                             db_path=tmp_path / "unused.db",
                             stop_event=threading.Event())
    never_done = threading.Event()  # 一度も set しない = スレッドは join を
                                     # 常に timeout させる
    threads = [threading.Thread(target=never_done.wait, daemon=True)
               for _ in range(3)]
    for t in threads:
        t.start()
    sup._active_threads = list(threads)

    timeout = 0.2
    start = time.monotonic()
    sup.join(timeout=timeout)
    elapsed = time.monotonic() - start

    # 修正前 (各スレッドへ timeout をまるごと渡す) なら 3*0.2=0.6s 消費する。
    # 修正後は共有 deadline なので 1*timeout + 小さな余裕に収まる。
    assert elapsed < timeout * 2, (
        f"join budget appears multiplied by thread count: "
        f"elapsed={elapsed:.3f}s, timeout={timeout}s, N=3")


def test_join_prunes_finished_threads(tmp_path):
    """`join()` 後、終了済みスレッドは `_active_threads` から取り除かれる
    (検収 B5 — tick を重ねても単調増加しない)。"""
    sup = ImproveSupervisor(capacity=1, root=tmp_path,
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(datetime(2026, 8, 22, 3, 0,
                                                        tzinfo=timezone.utc)),
                             db_path=tmp_path / "unused.db",
                             stop_event=threading.Event())
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    t.join()  # スレッドは既に終了済み
    sup._active_threads = [t]

    sup.join(timeout=1.0)

    assert sup._active_threads == []


# --- 10.12 節 Step 3: submit_manual (B16 — 9.5/9.7/10.12 のどこにも Step
# が無かった空洞の回収)。9.3 節の _RecordingFakeWorkerRunner をそのまま
# 使う (再定義しない — RW1/RW2)。`_FakeImproveLoop` は `prepare()` の
# 戻り値 `ctx` に文字列プレースホルダを使う (`_launch_slot` は `ctx` の
# 属性へ触れないため十分) が、`submit_manual` は `ctx.mission_id` を
# 読むため、本節専用に `.mission_id` を持つ最小 ctx を返すサブクラスを
# 用意する (新規命名、逸脱として申告)。

class _MissionIdCtx:
    def __init__(self, mission_id: int) -> None:
        self.mission_id = mission_id


class _ManualFakeImproveLoop(_FakeImproveLoop):
    """`submit_manual` 用の `_FakeImproveLoop` — `prepare()` の呼び出し
    引数 (`slot_key`/`on_ready`) を記録しつつ、`ctx.mission_id` を持つ
    ctx を返す。Tx-0 の slot claim は行わない (slot_key=None の契約どおり
    slot 行を一切作らない — 本物 `ImproveLoop.prepare` の `slot_key is
    None` 分岐 (10.2 節) をそのまま模す)。"""

    def __init__(self, conn, worker_runner, *, mission_id=999):
        super().__init__(conn, worker_runner, mission_id=mission_id)
        self.prepare_calls: list[dict] = []

    def prepare(self, *, slot_key, now, on_ready=None):
        self.prepare_calls.append({"slot_key": slot_key, "on_ready": on_ready})
        if on_ready is not None:
            self._worker_runner.on_ready = on_ready
        return (f"mission-{self._mission_id}",
                _MissionIdCtx(self._mission_id), self._worker_runner)


def test_submit_manual_returns_mission_id_without_slot_claim(conn):
    """B16 pin: 前任スタブは `raise NotImplementedError` のままだった。
    `submit_manual()` は `slot_key=None` で `prepare→run→commit` を直列に
    1 回ずつ呼び、`improve_wave_slots` に一切行を増やさず、戻り値は
    `prepare()` が払い出した mission_id (int) と一致する。"""
    events: list[str] = []
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(datetime(2026, 8, 22, 3, 0)),
                             db_path=Path("x"), stop_event=threading.Event())
    sup._conn_for_test = conn
    fake_loop = _ManualFakeImproveLoop(
        conn, _RecordingFakeWorkerRunner(events), mission_id=777)
    sup._improve_loop = fake_loop

    mission_id = sup.submit_manual()

    assert mission_id == 777
    assert isinstance(mission_id, int)
    assert events == ["run"]
    assert len(fake_loop.committed) == 1
    n_slots = conn.execute(
        "SELECT count(*) c FROM improve_wave_slots").fetchone()["c"]
    assert n_slots == 0


def test_submit_manual_prepares_with_slot_key_none_and_no_on_ready(conn):
    """R-D1/RW2: `submit_manual` は wave slot を持たないため
    `prepare(slot_key=None)` を呼び、`on_ready` は既定 (省略=None) の
    ままであることを確認する — mark_running する対象が無いことの pin。"""
    events: list[str] = []
    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(datetime(2026, 8, 22, 3, 0)),
                             db_path=Path("x"), stop_event=threading.Event())
    sup._conn_for_test = conn
    fake_loop = _ManualFakeImproveLoop(
        conn, _RecordingFakeWorkerRunner(events), mission_id=42)
    sup._improve_loop = fake_loop

    sup.submit_manual()

    assert fake_loop.prepare_calls == [{"slot_key": None, "on_ready": None}]

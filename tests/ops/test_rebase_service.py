"""HWM 付け直しの service: 入口 x 状態の表、値 CAS、排他、結果不明、監査の境界。

実 sqlite・実 StateStore・実 OpsService で動かす。判定は一箇所 (evaluate_rebase) で、
入口ごとの複写はなく、表の各行が preview の reason と POST の code を同時に固定する。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agentic_fx.ops.rebase_accounting as accounting
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.contracts import OrderStatus
from agentic_fx.core.transitions import TERMINAL
from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal, ShellCaller
from agentic_fx.ops.rebase_service import RebaseWiring
from agentic_fx.store.state import StateStore

APPROVER = Principal.APPROVER
UTC = timezone.utc
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # 月曜の開場中
START = 1_000_000.0
LOSS = 30_000.0


class _Clock:
    def now(self):
        return NOW


def _snapshot(conn, ts, *, balance, equity, hwm, cashflow=0.0, source="paper"):
    cur = conn.execute(
        "INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
        "VALUES (?,?,?,?,?,?)", (ts.isoformat(), balance, equity, hwm, cashflow, source))
    conn.commit()
    return cur.lastrowid


def _order(conn, status, pnl=0.0, fees=0.0):
    conn.execute(
        "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at,"
        "realized_pnl,fees_swap) VALUES ('USDJPY','long','market','day',?,?,?,?,?)",
        (status, NOW.isoformat(), NOW.isoformat(), pnl, fees))
    conn.commit()


@pytest.fixture
def rig(ops_env):
    env = ops_env
    env.wall.now = NOW
    conn = env.conn
    # 日次基準 (取引日の開始後の最初の行) と、損失後の最新 snapshot。
    _snapshot(conn, NOW - timedelta(hours=12), balance=START, equity=START, hwm=START)
    _order(conn, "closed", pnl=-LOSS)
    source_id = _snapshot(conn, NOW - timedelta(minutes=1), balance=START - LOSS,
                          equity=START - LOSS, hwm=START)
    env.state.update(kill_switch_latched=True)  # generation 1、ラッチ中
    core_lock = threading.RLock()
    wiring = RebaseWiring(core_lock=core_lock, broker=PaperBroker(conn, env.settings,
                                                                  _Clock()),
                          clock=_Clock())
    service = env.service(rebase=wiring)
    return SimpleNamespace(env=env, conn=conn, service=service, state=env.state,
                           core_lock=core_lock, wiring=wiring, source_id=source_id)


def _gen(rig):
    return rig.state.load().kill_switch_generation


def _preview(rig, generation=None):
    return rig.service.rebase_preview(APPROVER, _gen(rig) if generation is None
                                      else generation)


def _post(rig, token, generation=None):
    return rig.service.rebase_kill_switch(
        APPROVER, _gen(rig) if generation is None else generation, token)


def _ledger(conn):
    return conn.execute("SELECT * FROM kill_switch_hwm_rebases ORDER BY id").fetchall()


def _snapshots(conn):
    return conn.execute("SELECT * FROM account_snapshots ORDER BY id").fetchall()


def _rebase_requests(conn):
    return conn.execute("SELECT * FROM ops_requests WHERE endpoint LIKE '%rebase' "
                        "ORDER BY id").fetchall()


def _write_counts(conn):
    return (len(_ledger(conn)), len(_snapshots(conn)))


def _post_error(rig, token, generation=None) -> ErrorCode:
    with pytest.raises(OpsError) as raised:
        _post(rig, token, generation)
    return raised.value.code


# ------------------------------------------------------------------ 成功

def test_preview_is_read_only_and_creates_no_accepted_row(rig):
    before = _write_counts(rig.conn)
    state_before = rig.state._path.read_bytes()
    view = _preview(rig)
    assert view["eligible"] is True and view["reason"] is None
    assert len(view["preview_token"]) == 64
    assert view["generation"] == 1 and view["latched"] is True
    assert view["hwm"] == START and view["equity"] == START - LOSS
    assert view["new_hwm"] == START - LOSS
    assert view["cut"] == LOSS and view["cut_pct"] == pytest.approx(3.0)
    assert view["drawdown_pct"] == pytest.approx(3.0) and view["threshold_pct"] == 2.0
    assert view["past_count"] == 0 and view["count_after"] == 1
    assert view["cumulative_cut_after"] == LOSS
    assert view["additional_headroom"] == pytest.approx((START - LOSS) * 2.0 / 100)
    assert view["flat"] is True and view["cumulative_warning"] is False
    assert view["source_snapshot_id"] == rig.source_id
    assert _write_counts(rig.conn) == before
    assert rig.state._path.read_bytes() == state_before
    assert _rebase_requests(rig.conn) == []
    assert not rig.conn.in_transaction


def test_post_rebases_hwm_only_and_keeps_state(rig):
    state_before = rig.state.load()
    token = _preview(rig)["preview_token"]
    response = _post(rig, token)
    assert set(response) == {"rebase_id", "snapshot_id", "generation", "latched"}
    assert response["generation"] == 1 and response["latched"] is True
    after = rig.state.load()
    assert after == state_before  # ラッチ・世代・latched_at は変えない
    ledger = _ledger(rig.conn)
    assert len(ledger) == 1 and ledger[0]["id"] == response["rebase_id"]
    assert ledger[0]["accepted_audit_id"] == _rebase_requests(rig.conn)[0]["id"]
    assert ledger[0]["preview_token"] == token
    latest = rig.conn.execute(
        "SELECT * FROM account_snapshots ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    assert latest["id"] == response["snapshot_id"]
    assert latest["source"] == "manual_ks_rebase" and latest["hwm"] == latest["equity"]
    terminal = _rebase_requests(rig.conn)[-1]
    assert terminal["phase"] == "succeeded" and terminal["result_code"] == "ok"
    # 監査の本文は金額を持たない。
    assert set(json.loads(terminal["response_json"])) == {
        "rebase_id", "snapshot_id", "generation", "latched"}


def test_already_reset_generation_can_rebase_and_keeps_clear(rig):
    rig.state.reset_kill_switch(_gen(rig))
    state_before = rig.state.load()
    assert state_before.kill_switch_latched is False
    response = _post(rig, _preview(rig)["preview_token"])
    assert response["latched"] is False
    assert rig.state.load() == state_before
    assert len(_ledger(rig.conn)) == 1


def test_second_rebase_in_same_generation_after_new_loss(rig):
    _post(rig, _preview(rig)["preview_token"])
    again = _preview(rig)
    assert again["eligible"] is False and again["reason"] == "rebase_not_needed"
    assert _post_error(rig, "0" * 64) is ErrorCode.PREVIEW_CHANGED
    # 新しい確定損失で DD が閾値へ再び達すれば、同世代でも適用できる。
    new_equity = START - LOSS - 40_000.0
    _order(rig.conn, "closed", pnl=-40_000.0)
    _snapshot(rig.conn, NOW - timedelta(seconds=30), balance=new_equity, equity=new_equity,
              hwm=START - LOSS)
    view = _preview(rig)
    assert view["eligible"] is True and view["past_count"] == 1
    assert view["cumulative_cut_after"] == pytest.approx(LOSS + 40_000.0)
    _post(rig, view["preview_token"])
    assert len(_ledger(rig.conn)) == 2


# ------------------------------------------------------- 入口 x 状態の表

def _no_op(rig):
    return None


def _open_order(status):
    return lambda rig: _order(rig.conn, status)


def _stale(rig):
    rig.env.wall.now = NOW + timedelta(minutes=9, seconds=30)


def _snapshot_replaced(rig, **kwargs):
    _snapshot(rig.conn, NOW - timedelta(seconds=30), **kwargs)


TABLE = [
    ("eligible_latched", _no_op, None, None, None),
    ("flat_pending_order", _open_order("approval_pending"), None,
     "exposure_present", ErrorCode.PREVIEW_CHANGED),
    ("flat_open_order", _open_order("open"), None,
     "exposure_present", ErrorCode.PREVIEW_CHANGED),
    ("unknown_status", _open_order("some_future_status"), None,
     "exposure_present", ErrorCode.PREVIEW_CHANGED),
    ("source_too_old", lambda rig: setattr(rig.env.wall, "now", NOW + timedelta(minutes=10, seconds=1)),
     None, "account_snapshot_unavailable", ErrorCode.PREVIEW_CHANGED),
    ("source_at_boundary", lambda rig: setattr(rig.env.wall, "now", NOW + timedelta(minutes=9)),
     None, None, None),
    ("source_in_future", lambda rig: setattr(rig.env.wall, "now", NOW - timedelta(minutes=5)),
     None, "account_snapshot_unavailable", ErrorCode.PREVIEW_CHANGED),
    ("generation_mismatch", _no_op, 7, "generation_mismatch", ErrorCode.GENERATION_MISMATCH),
    ("threshold_unmet", lambda rig: _snapshot_replaced(
        rig, balance=START - LOSS, equity=START - LOSS, hwm=START - LOSS + 5_000.0),
     None, "rebase_not_needed", ErrorCode.PREVIEW_CHANGED),
    ("market_closed", lambda rig: setattr(rig.env.wall, "now",
                                          datetime(2026, 11, 14, 12, 0, tzinfo=UTC)),
     None, "market_closed", ErrorCode.MARKET_CLOSED),
    ("cashflow_nonzero", lambda rig: _snapshot_replaced(
        rig, balance=START - LOSS, equity=START - LOSS, hwm=START, cashflow=5.0),
     None, "accounting_inconsistent", ErrorCode.PREVIEW_CHANGED),
    ("source_is_rebase", lambda rig: _snapshot_replaced(
        rig, balance=START - LOSS, equity=START - LOSS, hwm=START - LOSS,
        source="manual_ks_rebase"),
     None, "rebase_not_needed", ErrorCode.PREVIEW_CHANGED),
    ("source_is_replay", lambda rig: _snapshot_replaced(
        rig, balance=START - LOSS, equity=START - LOSS, hwm=START, source="replay"),
     None, "account_snapshot_unavailable", ErrorCode.PREVIEW_CHANGED),
    ("source_equity_nonpositive", lambda rig: _snapshot_replaced(
        rig, balance=0.0, equity=0.0, hwm=START),
     None, "account_snapshot_unavailable", ErrorCode.PREVIEW_CHANGED),
    ("broker_disagrees_with_snapshot", lambda rig: _order(rig.conn, "closed", pnl=-1.0),
     None, "account_snapshot_changed", ErrorCode.PREVIEW_CHANGED),
    ("generation_zero", lambda rig: rig.state.save(
        rig.state.load().__class__(kill_switch_generation=0)), 0,
     "generation_unavailable", ErrorCode.PREVIEW_CHANGED),
    ("reset_marker_pending", lambda rig: rig.state._write_marker(1), None,
     "state_unavailable", ErrorCode.INVALID_STATE),
]


@pytest.mark.parametrize("name,mutate,expected_generation,reason,post_code", TABLE,
                         ids=[row[0] for row in TABLE])
def test_entrance_by_state_table(rig, name, mutate, expected_generation, reason, post_code):
    token = _preview(rig)["preview_token"]  # 事前に適格な表示を取り、状態を変えた後の同じ token で POST
    mutate(rig)
    generation = expected_generation
    counts = _write_counts(rig.conn)
    view = _preview(rig, generation)
    assert view["reason"] == reason
    assert view["eligible"] is (reason is None)
    assert (view["preview_token"] is None) is (reason is not None)
    if reason is None:
        # 状態が変わっていても、値が同じなら事前の token が通る。
        assert view["preview_token"] == token
        _post(rig, token, generation)
        assert len(_ledger(rig.conn)) == 1
        return
    assert _write_counts(rig.conn) == counts
    assert _post_error(rig, token, generation) is post_code
    assert _write_counts(rig.conn) == counts


@pytest.mark.parametrize("status", [s.value for s in OrderStatus])
def test_only_terminal_order_statuses_are_flat(rig, status):
    _order(rig.conn, status)
    # 注文を足しても損益は 0 なので broker 評価は一致したまま、flat だけが変わる。
    view = _preview(rig)
    terminal = {s.value for s in TERMINAL}
    assert view["eligible"] is (status in terminal)
    assert view["reason"] == (None if status in terminal else "exposure_present")


def test_one_open_order_among_closed_blocks(rig):
    for _ in range(3):
        _order(rig.conn, "closed")
    _order(rig.conn, "protection_pending")
    assert _preview(rig)["reason"] == "exposure_present"


def test_null_order_status_is_not_flat(rig):
    rig.conn.execute("PRAGMA foreign_keys=OFF")
    rig.conn.execute("DROP TABLE orders")
    rig.conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, realized_pnl REAL, "
        "fees_swap REAL)")
    rig.conn.execute("INSERT INTO orders(status,realized_pnl,fees_swap) VALUES (NULL,-30000,0)")
    rig.conn.commit()
    assert _preview(rig)["reason"] == "exposure_present"


def test_no_snapshot_is_unavailable(rig):
    rig.conn.execute("DELETE FROM account_snapshots")
    rig.conn.commit()
    assert _preview(rig)["reason"] == "account_snapshot_unavailable"
    assert _post_error(rig, "0" * 64) is ErrorCode.PREVIEW_CHANGED


def test_closed_market_wins_over_snapshot_freshness(rig):
    rig.env.wall.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)  # 土曜: 古い snapshot でもある
    assert _preview(rig)["reason"] == "market_closed"
    assert _post_error(rig, "0" * 64) is ErrorCode.MARKET_CLOSED


def test_wiring_not_injected_or_not_paper(rig):
    plain = rig.env.service()
    assert plain.rebase_preview(APPROVER, 1)["reason"] == "rebase_unavailable"
    with pytest.raises(OpsError) as raised:
        plain.rebase_kill_switch(APPROVER, 1, "0" * 64)
    assert raised.value.code is ErrorCode.UNAVAILABLE
    other = rig.env.service(rebase=RebaseWiring(
        core_lock=threading.RLock(), broker=SimpleNamespace(equity=lambda: (1.0, 1.0)),
        clock=_Clock()))
    assert other.rebase_preview(APPROVER, 1)["reason"] == "broker_not_supported"
    with pytest.raises(OpsError) as raised:
        other.rebase_kill_switch(APPROVER, 1, "0" * 64)
    assert raised.value.code is ErrorCode.UNAVAILABLE
    assert _write_counts(rig.conn) == (0, 2)


def test_autopilot_refuses_preview_and_post(rig):
    token = _preview(rig)["preview_token"]
    rig.state.update(autopilot=True)
    with pytest.raises(OpsError) as raised:
        _preview(rig)
    assert raised.value.code is ErrorCode.AUTOPILOT_RESTRICTED
    assert _post_error(rig, token) is ErrorCode.AUTOPILOT_RESTRICTED
    assert _ledger(rig.conn) == []


# ------------------------------------------------------------- 認可

@pytest.mark.parametrize("caller", [ShellCaller.SHELL, Principal.OPERATOR,
                                    Principal.OPS_BOT, Principal.OPS_WEB,
                                    Principal.OPS_WEB_WRITE, Principal.OPS_NOTIFY])
def test_only_the_approver_reaches_preview_and_post(rig, caller):
    with pytest.raises(OpsError) as raised:
        rig.service.rebase_preview(caller, 1)
    assert raised.value.code is ErrorCode.FORBIDDEN
    with pytest.raises(OpsError) as raised:
        rig.service.rebase_kill_switch(caller, 1, "0" * 64)
    assert raised.value.code is ErrorCode.FORBIDDEN
    assert _rebase_requests(rig.conn) == [] and _ledger(rig.conn) == []


@pytest.mark.parametrize("generation", [-1, "1", None, True, 1.0])
def test_generation_argument_is_validated(rig, generation):
    with pytest.raises(OpsError) as raised:
        rig.service.rebase_preview(APPROVER, generation)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(OpsError) as raised:
        rig.service.rebase_kill_switch(APPROVER, generation, "0" * 64)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("token", [None, 5, "", "xyz", "A" * 64, "0" * 63, "0" * 65])
def test_token_argument_is_validated_before_accept(rig, token):
    with pytest.raises(OpsError) as raised:
        rig.service.rebase_kill_switch(APPROVER, 1, token)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert _rebase_requests(rig.conn) == []


# --------------------------------------------------------- 値 CAS

def test_same_value_mtm_ticks_between_preview_and_post_do_not_block(rig):
    token = _preview(rig)["preview_token"]
    for seconds in (50, 40, 30):
        _snapshot(rig.conn, NOW - timedelta(seconds=seconds), balance=START - LOSS,
                  equity=START - LOSS, hwm=START)
    assert _post(rig, token)["generation"] == 1
    assert len(_ledger(rig.conn)) == 1


@pytest.mark.parametrize("change", ["hwm", "balance_equity", "threshold", "ledger",
                                    "latched", "starting_balance"])
def test_value_change_between_preview_and_post_is_rejected(rig, change):
    token = _preview(rig)["preview_token"]
    if change == "hwm":
        _snapshot_replaced(rig, balance=START - LOSS, equity=START - LOSS, hwm=START + 1.0)
    elif change == "balance_equity":
        _order(rig.conn, "closed", pnl=-1.0)
        _snapshot_replaced(rig, balance=START - LOSS - 1, equity=START - LOSS - 1, hwm=START)
    elif change == "threshold":
        rig.env.settings.risk.drawdown_kill_pct = 2.5
    elif change == "starting_balance":
        # 実効 starting_balance と DB の損益を同時に動かし、snapshot と broker は一致のまま。
        rig.env.settings.paper.starting_balance = START + 1.0
        rig.conn.execute("UPDATE orders SET realized_pnl=realized_pnl-1")
        _snapshot_replaced(rig, balance=START - LOSS, equity=START - LOSS, hwm=START)
    elif change == "latched":
        rig.state.reset_kill_switch(_gen(rig))
    elif change == "ledger":
        _post(rig, token)
        _order(rig.conn, "closed", pnl=-40_000.0)
        _snapshot(rig.conn, NOW - timedelta(seconds=20), balance=START - LOSS - 40_000,
                  equity=START - LOSS - 40_000, hwm=START - LOSS)
    before = len(_ledger(rig.conn))
    try:
        error = _post_error(rig, token)
    finally:
        rig.env.settings.risk.drawdown_kill_pct = 2.0
    assert error is ErrorCode.PREVIEW_CHANGED
    assert len(_ledger(rig.conn)) == before


def test_wrong_token_is_rejected_without_writes(rig):
    counts = _write_counts(rig.conn)
    assert _post_error(rig, "0" * 64) is ErrorCode.PREVIEW_CHANGED
    assert _write_counts(rig.conn) == counts
    terminal = _rebase_requests(rig.conn)[-1]
    assert terminal["phase"] == "failed" and terminal["result_code"] == "preview_changed"


def test_resending_a_used_token_is_rejected(rig):
    token = _preview(rig)["preview_token"]
    _post(rig, token)
    assert _post_error(rig, token) is ErrorCode.PREVIEW_CHANGED
    assert len(_ledger(rig.conn)) == 1


def test_two_concurrent_posts_apply_exactly_once(rig):
    token = _preview(rig)["preview_token"]
    outcomes: list = []
    barrier = threading.Barrier(2)

    def worker():
        conn_service = rig.service
        barrier.wait()
        try:
            outcomes.append(("ok", conn_service.rebase_kill_switch(APPROVER, 1, token)))
        except OpsError as exc:
            outcomes.append(("error", exc.code))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    assert [v for kind, v in outcomes if kind == "error"] == [ErrorCode.PREVIEW_CHANGED]
    assert len(_ledger(rig.conn)) == 1


# ------------------------------------------------- 結果不明 / 解放 / 診断

def test_commit_exception_is_outcome_unknown_and_ledger_resolves_it(rig, monkeypatch):
    token = _preview(rig)["preview_token"]
    real_commit = accounting._commit_rebase

    def commit_then_fail(conn):
        real_commit(conn)
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(accounting, "_commit_rebase", commit_then_fail)
    assert _post_error(rig, token) is ErrorCode.OUTCOME_UNKNOWN
    terminal = _rebase_requests(rig.conn)[-1]
    assert terminal["phase"] == "outcome_unknown"
    accepted = _rebase_requests(rig.conn)[0]
    assert len(_ledger(rig.conn)) == 1  # 台帳に accepted audit id の行がある = 成立
    assert _ledger(rig.conn)[0]["accepted_audit_id"] == accepted["id"]


def test_commit_exception_before_durability_leaves_no_ledger_row(rig, monkeypatch):
    token = _preview(rig)["preview_token"]

    def commit_fails(conn):
        raise sqlite3.OperationalError("disk full (simulated)")

    monkeypatch.setattr(accounting, "_commit_rebase", commit_fails)
    assert _post_error(rig, token) is ErrorCode.OUTCOME_UNKNOWN
    assert _ledger(rig.conn) == []
    assert _rebase_requests(rig.conn)[-1]["phase"] == "outcome_unknown"
    assert not rig.conn.in_transaction


class _ReleaseFailingStore(StateStore):
    """with の本体は通常どおり、解放の最後で通常例外を出す。"""

    @contextmanager
    def shared_guard(self, **kwargs):
        with super().shared_guard(**kwargs) as guard:
            yield guard
        raise RuntimeError("release exploded")


def test_exception_while_releasing_after_commit_keeps_success(rig, caplog):
    store = _ReleaseFailingStore(rig.state._path)
    service = rig.env.service(rebase=rig.wiring, state_store=store)
    token = service.rebase_preview(APPROVER, 1)["preview_token"]
    with caplog.at_level("WARNING"):
        response = service.rebase_kill_switch(APPROVER, 1, token)
    assert response["generation"] == 1
    assert len(_ledger(rig.conn)) == 1
    assert _rebase_requests(rig.conn)[-1]["phase"] == "succeeded"
    assert "rebase_release_failed" in caplog.text and "release exploded" not in caplog.text
    # core lock は解放済み (別 thread が取れる)。
    acquired: list[bool] = []
    t = threading.Thread(target=lambda: (acquired.append(rig.core_lock.acquire(timeout=2)),
                                         rig.core_lock.release()))
    t.start()
    t.join(5)
    assert acquired == [True]


def test_exception_before_commit_releases_core_lock_and_writes_nothing(rig, monkeypatch):
    token = _preview(rig)["preview_token"]
    real_insert = accounting.apply_rebase

    def failing(conn, **kwargs):
        def validate(db):
            kwargs["validate"](db)
            raise RuntimeError("validate exploded")
        return real_insert(conn, **{**kwargs, "validate": validate})

    from agentic_fx.ops import service as service_mod
    monkeypatch.setattr(service_mod, "apply_rebase", failing)
    with pytest.raises(RuntimeError, match="validate exploded"):
        _post(rig, token)
    assert _ledger(rig.conn) == [] and not rig.conn.in_transaction
    assert rig.core_lock.acquire(timeout=1)
    rig.core_lock.release()
    assert _rebase_requests(rig.conn)[-1]["result_code"] == "internal"


# ------------------------------------------------------ 取得停止

def test_core_lock_busy_times_out_without_writes(rig):
    token = _preview(rig)["preview_token"]
    held, release = threading.Event(), threading.Event()

    def holder():
        with rig.core_lock:
            held.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        start = rig.env.mono()
        error = _post_error(rig, token)
        assert error is ErrorCode.REQUEST_TIMEOUT
        assert rig.env.mono() - start < 15
    finally:
        release.set()
        thread.join(5)
    assert _ledger(rig.conn) == []
    # 何も適用されていないので、同じ token の再送が通る。
    assert _post(rig, token)["generation"] == 1


def test_state_flock_held_by_a_writer_times_out_without_writes(rig):
    token = _preview(rig)["preview_token"]
    core_held, release_core = threading.Event(), threading.Event()
    state_held, release_state = threading.Event(), threading.Event()

    def core_holder():
        with rig.core_lock:
            core_held.set()
            release_core.wait(30)

    def state_writer():
        with rig.state._exclusive():
            state_held.set()
            release_state.wait(30)

    threading.Thread(target=core_holder, daemon=True).start()
    assert core_held.wait(5)
    result: list = []
    poster = threading.Thread(
        target=lambda: result.append(_post_error(rig, token)), daemon=True)
    poster.start()
    # POST は受理と autopilot 確認を済ませ、core の取得待ちに入っている。
    threading.Event().wait(0.5)
    threading.Thread(target=state_writer, daemon=True).start()
    assert state_held.wait(5)
    release_core.set()  # POST は core を取り、state の共有 flock を 1 秒待って諦める
    poster.join(15)
    release_state.set()
    assert result == [ErrorCode.REQUEST_TIMEOUT]
    assert _ledger(rig.conn) == []
    assert rig.core_lock.acquire(timeout=1)
    rig.core_lock.release()
    assert _post(rig, token)["generation"] == 1


def test_stop_event_cancels_waiting_for_core(rig):
    token = _preview(rig)["preview_token"]
    held, release = threading.Event(), threading.Event()

    def holder():
        with rig.core_lock:
            held.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5)
    rig.service.stop_event.set()
    try:
        assert _post_error(rig, token) in (ErrorCode.UNAVAILABLE,)
    finally:
        release.set()
        thread.join(5)
    assert _ledger(rig.conn) == []


class _StaleAutopilotStore(StateStore):
    """入口の確認 (load) だけ autopilot が無いと答える。保持中の直接検査が拒むかを見る。"""

    def load(self, **kwargs):
        from dataclasses import replace
        return replace(super().load(**kwargs), autopilot=False)


def test_autopilot_turned_on_after_the_entry_check_is_caught_under_the_guard(rig):
    token = _preview(rig)["preview_token"]
    rig.state.update(autopilot=True)
    store = _StaleAutopilotStore(rig.state._path)
    service = rig.env.service(rebase=rig.wiring, state_store=store)
    with pytest.raises(OpsError) as raised:
        service.rebase_kill_switch(APPROVER, 1, token)
    assert raised.value.code is ErrorCode.AUTOPILOT_RESTRICTED
    assert _ledger(rig.conn) == []


# ------------------------------------------------------- 実 UDS の応答

def test_over_the_real_uds_preview_post_and_the_two_new_errors(rig):
    from ._api import make_api
    api = make_api(rig.env, service_kwargs={"rebase": rig.wiring})
    try:
        client = api.client(Principal.APPROVER)
        view = client.get("/v1/killswitch/rebase-preview?expected_generation=1")
        assert view.status_code == 200
        data = view.json()["data"]
        assert data["eligible"] is True and len(data["preview_token"]) == 64
        stale = client.post("/v1/killswitch/rebase",
                            json={"expected_generation": 1, "preview_token": "0" * 64})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "preview_changed"
        invalid = client.post("/v1/killswitch/rebase",
                              json={"expected_generation": 1, "preview_token": "nope"})
        assert invalid.status_code == 400
        assert api.client(Principal.OPERATOR).get(
            "/v1/killswitch/rebase-preview?expected_generation=1").status_code == 403
        assert api.client(Principal.OPERATOR).post(
            "/v1/killswitch/rebase",
            json={"expected_generation": 1, "preview_token": "0" * 64}).status_code == 403
        done = client.post("/v1/killswitch/rebase",
                           json={"expected_generation": 1,
                                 "preview_token": data["preview_token"]})
        assert done.status_code == 200
        assert set(done.json()["data"]) - {"display_timezone"} == {
            "rebase_id", "snapshot_id", "generation", "latched"}
        rig.env.wall.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
        closed = client.post("/v1/killswitch/rebase",
                             json={"expected_generation": 1, "preview_token": "0" * 64})
        assert closed.status_code == 409
        assert closed.json()["error"]["code"] == "market_closed"
    finally:
        for c in api.clients:
            c.close()
        api.server.stop(5.0)


def test_success_projects_only_the_existing_ops_activity_and_terminal_event(rig):
    _post(rig, _preview(rig)["preview_token"])
    text = rig.env.activity_path.read_text(encoding="utf-8")
    assert "ops_killswitch_rebase" in text and "kill_switch_hwm_rebased" not in text
    request_id = _rebase_requests(rig.conn)[0]["id"]
    assert f"audit={request_id} result=ok" in text
    codes = [r["code"] for r in rig.conn.execute("SELECT code FROM ops_events").fetchall()]
    assert "ops_request_terminal" in codes
    assert "guard_state_changed" not in codes and "risk_budget_rebased" not in codes
    fields = [json.loads(r["fields_json"]) for r in rig.conn.execute(
        "SELECT fields_json FROM ops_events WHERE code='ops_request_terminal'").fetchall()]
    assert fields[-1]["endpoint_code"] == "killswitch_rebase"
    assert not any(k in json.dumps(fields) for k in ("hwm", "cut", "equity"))


# ------------------------------------------------ 保持予算と transaction 内の時刻・source

def _spy_commit(monkeypatch):
    calls = []
    real = accounting._commit_rebase
    monkeypatch.setattr(accounting, "_commit_rebase",
                        lambda conn: (calls.append(1), real(conn))[1])
    return calls


def test_slow_evaluation_past_the_hold_budget_does_not_commit(rig, monkeypatch):
    from agentic_fx.ops import rebase_service
    token = _preview(rig)["preview_token"]
    commits = _spy_commit(monkeypatch)
    real = rebase_service.evaluate_rebase

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        rig.env.mono.advance(2.0)  # 1 秒の保持予算を評価だけで使い切る
        return result

    monkeypatch.setattr(rebase_service, "evaluate_rebase", slow)
    counts = _write_counts(rig.conn)
    assert _post_error(rig, token) is ErrorCode.REQUEST_TIMEOUT
    assert commits == [] and _write_counts(rig.conn) == counts
    assert not rig.conn.in_transaction
    assert _rebase_requests(rig.conn)[-1]["result_code"] == "request_timeout"


def test_slow_insert_past_the_hold_budget_does_not_commit(rig, monkeypatch):
    token = _preview(rig)["preview_token"]
    commits = _spy_commit(monkeypatch)
    real = accounting.daily_start_equity
    calls = []

    def slow(conn, now):
        calls.append(1)
        value = real(conn, now)
        if len(calls) == 2:  # 2 行の INSERT の後の事後検査で時間がかかる
            rig.env.mono.advance(2.0)
        return value

    monkeypatch.setattr(accounting, "daily_start_equity", slow)
    counts = _write_counts(rig.conn)
    assert _post_error(rig, token) is ErrorCode.REQUEST_TIMEOUT
    assert len(calls) == 2 and commits == []
    assert _write_counts(rig.conn) == counts and not rig.conn.in_transaction


@pytest.mark.parametrize("moved_to, code", [
    (datetime(2026, 11, 21, 12, 0, tzinfo=UTC), ErrorCode.MARKET_CLOSED),  # 土曜
    (NOW + timedelta(minutes=12), ErrorCode.PREVIEW_CHANGED),  # snapshot が 10 分超
])
def test_time_is_taken_again_inside_the_transaction(rig, monkeypatch, moved_to, code):
    from agentic_fx.ops import service as service_mod
    token = _preview(rig)["preview_token"]
    real = service_mod.apply_rebase

    def late(conn, **kwargs):
        rig.env.wall.now = moved_to  # BEGIN IMMEDIATE を待つ間に時が進んだ
        return real(conn, **kwargs)

    monkeypatch.setattr(service_mod, "apply_rebase", late)
    counts = _write_counts(rig.conn)
    assert _post_error(rig, token) is code
    assert _write_counts(rig.conn) == counts


def test_the_validated_instant_is_the_applied_at(rig, monkeypatch):
    from agentic_fx.ops import service as service_mod
    token = _preview(rig)["preview_token"]
    real = service_mod.apply_rebase

    def late(conn, **kwargs):
        rig.env.wall.now = NOW + timedelta(minutes=2)
        return real(conn, **kwargs)

    monkeypatch.setattr(service_mod, "apply_rebase", late)
    _post(rig, token)
    assert _ledger(rig.conn)[0]["applied_at"] == (NOW + timedelta(minutes=2)).isoformat()


def test_same_value_snapshot_right_before_begin_immediate_still_succeeds(rig, monkeypatch):
    from agentic_fx.ops import service as service_mod
    token = _preview(rig)["preview_token"]
    real = service_mod.apply_rebase

    def late(conn, **kwargs):
        _snapshot(rig.conn, NOW - timedelta(seconds=20), balance=START - LOSS,
                  equity=START - LOSS, hwm=START)
        return real(conn, **kwargs)

    monkeypatch.setattr(service_mod, "apply_rebase", late)
    _post(rig, token)
    newest = max(row["id"] for row in _snapshots(rig.conn) if row["source"] == "paper")
    assert _ledger(rig.conn)[0]["source_snapshot_id"] == newest != rig.source_id


def test_changed_value_snapshot_right_before_begin_immediate_is_rejected(rig, monkeypatch):
    from agentic_fx.ops import service as service_mod
    token = _preview(rig)["preview_token"]
    real = service_mod.apply_rebase

    def late(conn, **kwargs):
        _snapshot(rig.conn, NOW - timedelta(seconds=20), balance=START - LOSS,
                  equity=START - LOSS, hwm=START + 5.0)
        return real(conn, **kwargs)

    monkeypatch.setattr(service_mod, "apply_rebase", late)
    assert _post_error(rig, token) is ErrorCode.PREVIEW_CHANGED
    assert _ledger(rig.conn) == []
    assert [r["source"] for r in _snapshots(rig.conn)].count("manual_ks_rebase") == 0

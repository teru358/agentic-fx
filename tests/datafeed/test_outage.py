"""OutageStateMachine (価格源不通の検出・永続化) の観測・永続化。

fake transport なし (bridge 通信は datafeed/test_ingest.py 側)。ここでは
`Ingest.prepare()` が返す `IngestTickReport` を手組みし、tmp SQLite だけで
`OutageStateMachine.observe()` の遷移・永続化を検証する。
"""
from datetime import datetime, timedelta, timezone

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import (
    EMPTY_REPORT,
    IngestTickReport,
    OutageStateMachine,
    count_unprocessed_positions,
    format_unprocessed_positions,
    unprocessed_positions_by_pair,
)
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
HARD_KEYS = frozenset({(PAIR, "1m"), (PAIR, "1h")})
WIDTHS = {"1m": timedelta(minutes=1), "1h": timedelta(minutes=60)}
GRACE = timedelta(seconds=30)


def _machine(conn, *, activity=None, **kw):
    return OutageStateMachine(conn, hard_keys=HARD_KEYS, interval_widths=WIDTHS,
                              grace=GRACE, storage_source="mt5-live",
                              activity=activity, **kw)


def _report(*, failed=(), succeeded=(), empty=()):
    return IngestTickReport(
        attempted=frozenset(succeeded) | frozenset(k for k, _ in failed),
        succeeded=frozenset(succeeded),
        failed=frozenset(failed),
        deferred=frozenset(),
        empty=frozenset(empty))


def _seed_bar(conn, pair, interval, bar_time, *, source="mt5-live"):
    upsert_cache_bars(conn, [Bar(pair, interval, bar_time, 1, 1, 1, 1, 1)],
                      source=source)


def _db(tmp_path):
    conn = connect(tmp_path / "outage.db")
    init_db(conn)
    return conn


def test_first_hard_failure_transitions_immediately_without_waiting_two_ticks(tmp_path):
    """1 回目の失敗で直ちに degraded に遷移する。confirmed は 0 のまま。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    now = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    report = _report(failed=[((PAIR, "1m"), "ConnectError")],
                     succeeded=[(PAIR, "1h")])
    state = m.observe(now, report)
    assert state == "degraded"
    row = m.status()
    assert row["epoch"] == 1
    assert row["confirmed"] == 0
    assert row["entered_degraded_at"] is not None


def test_budget_exhausted_alone_does_not_degrade(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    now = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    # budget_exhausted な tick: 何も attempted/failed に無い (deferred のみ)
    report = IngestTickReport(attempted=frozenset(), succeeded=frozenset(),
                              failed=frozenset(), deferred=frozenset({(PAIR, "1h")}),
                              empty=frozenset())
    assert m.observe(now, report) == "ready"


def test_stall_formula_single_grace_no_double_counting_1m_and_1h(tmp_path):
    """停滞式 expected = watermark + 2*width + grace の単一式 (二重加算しない)。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    # 10:01:00 はまだ ready (10:01:30 を過ぎていない)
    assert m.observe(datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc),
                     _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])) == "ready"
    # 10:02:00 (1m の停滞式を過ぎた最初の tick) で degraded
    assert m.observe(datetime(2026, 9, 24, 10, 2, 0, tzinfo=timezone.utc),
                     _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])) == "degraded"


def test_stall_formula_1h_boundary(tmp_path):
    conn = _db(tmp_path)
    # 1m 側は両方の観測時刻で健全であり続けるように 2 本用意しておく —
    # そうしないと 1m の停滞だけで degraded になり、1h の式を検証できない。
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 58, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    assert m.observe(datetime(2026, 9, 24, 11, 0, 0, tzinfo=timezone.utc),
                     _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])) == "ready"
    assert m.observe(datetime(2026, 9, 24, 11, 1, 0, tzinfo=timezone.utc),
                     _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])) == "degraded"


def test_stall_formula_skips_weekend_until_next_bar_confirmation(tmp_path):
    conn = _db(tmp_path)
    machine = OutageStateMachine(
        conn, hard_keys=frozenset({(PAIR, "1m"), (PAIR, "15m")}),
        interval_widths={"1m": timedelta(minutes=1), "15m": timedelta(minutes=15)},
        grace=GRACE, storage_source="mt5-live")
    friday_15m = datetime(2026, 9, 25, 20, 45, tzinfo=timezone.utc)
    assert not machine._is_stalled(datetime(2026, 9, 27, 21, 0, 3, tzinfo=timezone.utc), "15m", friday_15m)
    assert not machine._is_stalled(datetime(2026, 9, 27, 21, 15, 29, tzinfo=timezone.utc), "15m", friday_15m)
    # 期限ちょうどはまだ停滞ではない (確定規則は `<= cutoff` で足を受け入れる側)
    assert not machine._is_stalled(datetime(2026, 9, 27, 21, 15, 30, tzinfo=timezone.utc), "15m", friday_15m)
    assert machine._is_stalled(datetime(2026, 9, 27, 21, 15, 31, tzinfo=timezone.utc), "15m", friday_15m)
    friday_1m = datetime(2026, 9, 25, 20, 59, tzinfo=timezone.utc)
    assert not machine._is_stalled(datetime(2026, 9, 27, 21, 1, 29, tzinfo=timezone.utc), "1m", friday_1m)
    assert machine._is_stalled(datetime(2026, 9, 27, 21, 1, 31, tzinfo=timezone.utc), "1m", friday_1m)


def test_two_consecutive_hard_problem_ticks_confirm_exactly_once(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 24, 10, 3, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)
    assert m.status()["confirmed"] == 0
    m.observe(t2, failed)
    assert m.status()["confirmed"] == 1
    confirmed_events = [l for l in log.tail(50) if "data_outage_degraded" in l]
    assert len(confirmed_events) == 1
    m.observe(t3, failed)
    # 3 tick 目も confirmed のままだが、activity は再送しない
    confirmed_events = [l for l in log.tail(50) if "data_outage_degraded" in l]
    assert len(confirmed_events) == 1
    # degraded への遷移そのものも t1 の 1 回だけで、以降は再送しない
    degraded_events = [l for l in log.tail(50)
                       if "\tdatafeed_degraded\t" in l]
    assert len(degraded_events) == 1


def test_recovering_hard_keys_does_not_auto_return_to_ready(tmp_path):
    """成功しても自動では ready に戻らない (人間の resume 待ち)。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t1, failed) == "degraded"
    # 復旧: 新しい 1m バーが確定し、停滞式も満たさなくなった状態を作る
    # (cutoff = now - width - grace が bar_time を追い越すのに十分な間隔をあける)
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    assert m.observe(t2, healthy) == "degraded"


def test_recovery_while_degraded_writes_awaiting_resume_activity_once(tmp_path):
    # 手動復帰 (自動復帰なし) の経路を固定する
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log, auto_resume_when_flat=False)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    m.observe(t2, healthy)
    # 継続して健全であり続ける (新しいバーが毎 tick 届く) 状況を模す
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc))
    t3 = datetime(2026, 9, 24, 10, 8, tzinfo=timezone.utc)
    m.observe(t3, healthy)
    awaiting = [l for l in log.tail(50) if "datafeed_recovered_awaiting_resume" in l]
    assert len(awaiting) == 1


def test_state_persists_across_a_fresh_instance_on_the_same_connection(tmp_path):
    """再起動相当: 新しい OutageStateMachine を同じ conn で作っても degraded を保つ。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m1 = _machine(conn)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m1.observe(t1, failed) == "degraded"
    epoch_before = m1.status()["epoch"]

    m2 = _machine(conn)  # プロセス再起動を模した新インスタンス
    assert m2.state == "degraded"
    assert m2.status()["epoch"] == epoch_before
    # 新インスタンスでも観測を続けられる (gap_start 等は失われていない)
    t2 = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    assert m2.observe(t2, failed) == "degraded"
    assert m2.status()["epoch"] == epoch_before


def test_flap_does_not_bump_epoch_or_gap_start(tmp_path):
    """flap (再失敗) しても epoch/gap_start は同一 episode 内で不変。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)
    gap_start_1 = m._gap_start(PAIR, "1m", 1)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    m.observe(t2, _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")]))
    t3 = datetime(2026, 9, 24, 10, 3, tzinfo=timezone.utc)
    m.observe(t3, failed)  # flap: 再び失敗
    row = m.status()
    assert row["epoch"] == 1
    assert m._gap_start(PAIR, "1m", 1) == gap_start_1


def test_watermark_reads_are_scoped_by_storage_source(tmp_path):
    """primary が不通でも別 source の残存行を健全の根拠にしない。"""
    conn = _db(tmp_path)
    # mt5-live (primary) は古いまま、yfinance だけ新しい行が残っている
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc),
             source="mt5-live")
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
             source="yfinance")
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc),
             source="mt5-live")
    m = _machine(conn)
    now = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    # 1m の ingest 自体は succeeded だったことにしても (例えば「別 source から
    # 取ってしまった」誤設定を模す)、mt5-live 側の watermark 停滞は検出される
    state = m.observe(now, _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")]))
    assert state == "degraded"


def test_ingest_tick_report_is_not_affected_by_last_errors_residue(tmp_path):
    """last_errors の残留に影響されず、成功すれば report.succeeded に載り
    report.failed は空になる。"""
    from agentic_fx.datafeed.ingest import Ingest
    from types import SimpleNamespace

    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    calls = {"n": 0}

    def fetch(pair, interval, start, end, *, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("offline")
        return []

    settings = SimpleNamespace(
        pairs=["USDJPY"],
        datafeed=SimpleNamespace(primary="yfinance", intervals=["1m", "1h"],
                                 primary_intervals=["1h"], ingest_budget_sec=10,
                                 closed_bar_grace_sec=0))
    ingest = Ingest(settings, fetch=fetch)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    count, report = ingest.prepare(now, conn)
    assert any(key == ("USDJPY", "1m") for key, _ in report.failed)
    # last_errors は残留する (既存挙動)
    assert ("USDJPY", "1m") in ingest.last_errors
    count2, report2 = ingest.prepare(now + timedelta(seconds=1), conn)
    assert report2.failed == frozenset()
    assert ("USDJPY", "1m") in report2.succeeded


def test_migration_is_idempotent_for_outage_tables(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    init_db(conn)  # 2 回目 (再起動相当) — 例外が出たら red
    cols_state = {r["name"] for r in conn.execute(
        "PRAGMA table_info(datafeed_outage_state)")}
    cols_gap = {r["name"] for r in conn.execute(
        "PRAGMA table_info(datafeed_outage_gap)")}
    assert "state" in cols_state and "epoch" in cols_state
    assert "gap_start" in cols_gap and "replay_through" in cols_gap


# ---- count_unprocessed_positions (保守的な近似規則) --------------------


def _insert_open(conn, pair, filled_at):
    return orders.insert(conn, pair=pair, direction="buy", entry_type="market",
                         horizon="swing", status="open", now=filled_at,
                         filled_at=filled_at.isoformat())


def _insert_pending(conn, pair, created_at):
    return orders.insert(conn, pair=pair, direction="buy", entry_type="limit",
                         horizon="swing", status="pending_fill", now=created_at)


def test_unprocessed_positions_uses_conservative_lower_bound_not_entered_degraded_at(tmp_path):
    """`gap_start=09:59`, `entered_degraded_at=10:01`, 10:02 復旧のケースで、
    10:00 の bar が既に確定していることを保守規則 (`gap_start` 基準) が
    正しく捉える (`entered_degraded_at` 基準では見逃してしまう反例)。"""
    conn = _db(tmp_path)
    gap_start = datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    # 10:00 の 1m 足はすでに確定している
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    filled_at = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)  # outage より前から存在
    _insert_open(conn, PAIR, filled_at)

    count = count_unprocessed_positions(
        conn, hard_keys=HARD_KEYS, epoch=1,
        gap_start_fn=lambda pair, interval: gap_start if interval == "1m" else None,
        now=now, storage_source="mt5-live", grace=GRACE, interval_widths=WIDTHS)
    assert count == 1  # gap_start 基準なら 10:00 の未処理足を検出する


def test_pending_fill_lower_bound_is_created_at_not_filled_at(tmp_path):
    conn = _db(tmp_path)
    gap_start = datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    created_at = datetime(2026, 9, 24, 10, 0, 30, tzinfo=timezone.utc)
    _insert_pending(conn, PAIR, created_at)

    count = count_unprocessed_positions(
        conn, hard_keys=HARD_KEYS, epoch=1,
        gap_start_fn=lambda pair, interval: gap_start if interval == "1m" else None,
        now=now, storage_source="mt5-live", grace=GRACE, interval_widths=WIDTHS)
    # created_at (10:00:30) 以降・latest_confirmed (10:00) 以前の bar は
    # 存在しないので未処理はゼロ (created_at がその足より後なので数えない)
    assert count == 0


EUR = "EURUSD"
HARD_KEYS_TWO_PAIRS = frozenset(
    {(EUR, "1m"), (EUR, "1h"), (PAIR, "1m"), (PAIR, "1h")})


def test_unprocessed_positions_by_pair_only_lists_the_pair_inside_the_gap(tmp_path):
    """2 pair に建玉があっても、実際に不通 gap 内にあるのは片方だけなら
    内訳にはその pair だけが現れ、bars 数は具体値と一致する。"""
    conn = _db(tmp_path)
    gap_start = datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc)
    _seed_bar(conn, EUR, "1m", datetime(2026, 9, 24, 10, 3, tzinfo=timezone.utc))
    _insert_open(conn, EUR, datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc))
    _insert_pending(conn, EUR, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    # USDJPY にも OPEN 建玉はあるが、gap_start_fn が None を返す (不通 gap の
    # 外側) ので内訳には現れないはず。
    _insert_open(conn, PAIR, datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc))

    by_pair = unprocessed_positions_by_pair(
        conn, hard_keys=HARD_KEYS_TWO_PAIRS, epoch=1,
        gap_start_fn=lambda pair, interval:
            gap_start if (pair, interval) == (EUR, "1m") else None,
        now=now, storage_source="mt5-live", grace=GRACE, interval_widths=WIDTHS)

    assert set(by_pair) == {EUR}
    # lower_bound = next_expected_trading_time(09:59) = 10:00、
    # latest_confirmed = 10:03 (cutoff = 10:05 - 1min - 30s = 10:03:30)。
    # open (filled_at 09:30 以前) の floor = 10:00 → bars = (10:03-10:00)/1min+1 = 4。
    assert by_pair[EUR] == {"positions": 2, "bars": 4}


def test_no_open_or_pending_positions_means_zero_unprocessed(tmp_path):
    conn = _db(tmp_path)
    gap_start = datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    count = count_unprocessed_positions(
        conn, hard_keys=HARD_KEYS, epoch=1,
        gap_start_fn=lambda pair, interval: gap_start if interval == "1m" else None,
        now=now, storage_source="mt5-live", grace=GRACE, interval_widths=WIDTHS)
    assert count == 0


# ---- resume request consumption (tick-local, single lock section) --------


def test_resume_request_is_rejected_when_unprocessed_positions_remain(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)  # -> degraded, gap_start(1m) = 09:59

    # 復旧: watermark は健全だが、outage 期間中に確定した 10:00 の足を
    # まだ処理していない OPEN 建玉が残っている。
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    filled_at = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market",
                  horizon="swing", status="open", now=filled_at,
                  filled_at=filled_at.isoformat())

    m.request_resume(datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc), acknowledge=False)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    state = m.observe(t2, healthy)

    assert state == "degraded"
    assert m.status()["resume_requested_at"] is None
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert "unprocessed_positions" in rejected[0]
    # pair 名・推定未処理足数を pin する。lower_bound = 10:00
    # (next_expected_trading_time(09:59))、latest_confirmed = 10:05
    # (cutoff = 10:07 - 1min - 30s = 10:05:30) → bars = (10:05-10:00)/1min+1 = 6。
    assert f"{PAIR}: 1 positions / ~6 bars" in rejected[0]


def test_resume_request_is_accepted_with_acknowledge_despite_unprocessed_positions(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)

    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    filled_at = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market",
                  horizon="swing", status="open", now=filled_at,
                  filled_at=filled_at.isoformat())

    m.request_resume(datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc), acknowledge=True)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    state = m.observe(t2, healthy)

    assert state == "ready"
    assert m.status()["resume_requested_at"] is None
    accepted = [l for l in log.tail(50) if "data_resume_accepted" in l]
    assert len(accepted) == 1
    assert "acknowledge=True" in accepted[0]


def test_resume_request_is_accepted_when_no_positions_are_open(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)

    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    # OPEN/PENDING_FILL は無い

    m.request_resume(datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc), acknowledge=False)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    state = m.observe(t2, healthy)

    assert state == "ready"
    assert m.status()["resume_requested_at"] is None
    accepted = [l for l in log.tail(50) if "data_resume_accepted" in l]
    assert len(accepted) == 1


def test_resume_request_is_rejected_when_hard_key_not_attempted_since_last_failure(
        tmp_path):
    """一度 failed した hard key がその後 succeeded を一度も観測しないまま
    not_attempted (backoff で今回は probe しなかった) tick を迎えても、
    停滞式をまだ満たしていなければ拒否されるべき (以前は誤って受理していた)。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t1, failed) == "degraded"

    m.request_resume(datetime(2026, 9, 24, 10, 1, 10, tzinfo=timezone.utc),
                     acknowledge=False)
    # 1m はまだ一度も succeeded を観測していないが、この tick では停滞式も
    # 満たさない (not_attempted であって failed でもない、単なる backoff)。
    not_attempted = IngestTickReport(
        attempted=frozenset({(PAIR, "1h")}), succeeded=frozenset({(PAIR, "1h")}),
        failed=frozenset(), deferred=frozenset(), empty=frozenset())
    t2 = datetime(2026, 9, 24, 10, 1, 20, tzinfo=timezone.utc)
    state = m.observe(t2, not_attempted)

    assert state == "degraded"
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert "watermark_unhealthy" in rejected[0]
    assert f"{PAIR}:1m" in rejected[0]


def test_resume_request_is_accepted_after_succeeded_tick_then_not_attempted(tmp_path):
    """succeeded を一度観測できていれば、その後 not_attempted の tick でも
    (据え置きにより) 健全とみなして受理する。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)

    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 7, 0, tzinfo=timezone.utc)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    m.observe(t2, healthy)  # 両 hard key succeeded を観測

    m.request_resume(datetime(2026, 9, 24, 10, 7, 10, tzinfo=timezone.utc),
                     acknowledge=False)
    # 次の tick では 1m の次の足がまだ確定していないため probe しない
    # (not_attempted) — それでも停滞式はまだ満たさない。
    not_attempted = IngestTickReport(
        attempted=frozenset({(PAIR, "1h")}), succeeded=frozenset({(PAIR, "1h")}),
        failed=frozenset(), deferred=frozenset(), empty=frozenset())
    t3 = datetime(2026, 9, 24, 10, 7, 20, tzinfo=timezone.utc)
    state = m.observe(t3, not_attempted)

    assert state == "ready"
    accepted = [l for l in log.tail(50) if "data_resume_accepted" in l]
    assert len(accepted) == 1


def test_resume_request_is_rejected_when_only_deferred_since_failure(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    t1 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)

    m.request_resume(datetime(2026, 9, 24, 10, 1, 10, tzinfo=timezone.utc),
                     acknowledge=False)
    deferred_only = IngestTickReport(
        attempted=frozenset(), succeeded=frozenset(), failed=frozenset(),
        deferred=frozenset({(PAIR, "1m")}), empty=frozenset())
    t2 = datetime(2026, 9, 24, 10, 1, 20, tzinfo=timezone.utc)
    state = m.observe(t2, deferred_only)

    assert state == "degraded"
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert f"{PAIR}:1m" in rejected[0]


def test_resume_request_is_rejected_right_after_process_restart_with_not_attempted_tick(
        tmp_path):
    """再起動相当 (新インスタンス) では `_last_attempt_ok` が失われるため、
    not_attempted の key は「未確認」として拒否されるべき。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m1 = _machine(conn)
    t1 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m1.observe(t1, failed)

    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 7, 0, tzinfo=timezone.utc)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    m1.observe(t2, healthy)  # m1 では succeeded 済み

    log = ActivityLog(tmp_path / "activity.log")
    m2 = _machine(conn, activity=log)  # プロセス再起動相当。メモリは空に戻る
    m2.request_resume(datetime(2026, 9, 24, 10, 7, 10, tzinfo=timezone.utc),
                      acknowledge=False)
    not_attempted = IngestTickReport(
        attempted=frozenset({(PAIR, "1h")}), succeeded=frozenset({(PAIR, "1h")}),
        failed=frozenset(), deferred=frozenset(), empty=frozenset())
    t3 = datetime(2026, 9, 24, 10, 7, 20, tzinfo=timezone.utc)
    state = m2.observe(t3, not_attempted)

    assert state == "degraded"
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert f"{PAIR}:1m" in rejected[0]


def test_resume_request_is_rejected_when_watermark_still_unhealthy(tmp_path):
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    m = _machine(conn)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(t1, failed)

    m.request_resume(datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc), acknowledge=True)
    # 復旧しないまま次 tick を迎える (依然 failed)
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    state = m.observe(t2, failed)

    assert state == "degraded"
    assert m.status()["resume_requested_at"] is None


def test_resume_request_is_rejected_when_epoch_crosses_with_stale_success_memo(tmp_path):
    """前 episode で succeeded を観測していた hard key でも、新しい episode
    (epoch) では改めて succeeded を観測しない限り健全とみなさない。片方の
    key が新 episode で復旧しても、もう片方が新 episode では一度も
    succeeded を観測していなければ resume は拒否される。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)

    # 旧 episode (ready) 中に両方 succeeded を観測しておく。
    t0 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    both_healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    assert m.observe(t0, both_healthy) == "ready"

    # 片方 (1m) だけ failed で degraded に遷移 (新 epoch 開始)。1h はこの
    # tick では試みていない。
    t1 = datetime(2026, 9, 24, 10, 0, 10, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")])
    assert m.observe(t1, failed) == "degraded"

    # 失敗した 1m だけ succeeded を観測。1h は新 epoch で一度も
    # succeeded を観測していない (not_attempted のまま)。
    m.request_resume(datetime(2026, 9, 24, 10, 0, 15, tzinfo=timezone.utc),
                     acknowledge=False)
    only_1m_recovered = _report(succeeded=[(PAIR, "1m")])
    t2 = datetime(2026, 9, 24, 10, 0, 20, tzinfo=timezone.utc)
    state = m.observe(t2, only_1m_recovered)

    assert state == "degraded"
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert "watermark_unhealthy" in rejected[0]
    assert f"unconfirmed_keys={PAIR}:1h" in rejected[0]


def test_resume_request_is_rejected_when_recovery_tick_is_empty_response(tmp_path):
    """failed の次 tick で succeeded かつ 0 本 (empty) だった key は、
    watermark がまだ停滞式を満たしていなくても resume の健全証拠にしない。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)

    t1 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t1, failed) == "degraded"

    m.request_resume(datetime(2026, 9, 24, 10, 0, 10, tzinfo=timezone.utc),
                     acknowledge=False)
    # 応答自体は succeeded (例外なし) だが 0 本 — watermark はまだ停滞式を
    # 満たしていない (9:59 + 2*1min + 30s = 10:01:30 より前)。
    empty_recovery = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")], empty=[(PAIR, "1m")])
    t2 = datetime(2026, 9, 24, 10, 0, 20, tzinfo=timezone.utc)
    state = m.observe(t2, empty_recovery)

    assert state == "degraded"
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert "watermark_unhealthy" in rejected[0]
    assert f"unconfirmed_keys={PAIR}:1m" in rejected[0]


def test_prior_non_empty_success_does_not_survive_a_later_empty_tick(tmp_path):
    """degraded 中に一度は非 empty 成功を観測した key でも、直後の tick が
    empty (0 本) 応答なら resume の健全証拠として失効する。stalled 式が
    まだ境界を過ぎていない (watermark はまだ新鮮) 状況でも、この tick が
    empty だった key は unconfirmed とみなされ resume は拒否される。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)

    # 1m が failed で degraded に遷移 (新 epoch)。
    t1 = datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t1, failed) == "degraded"

    # 次 tick で 1m の watermark を進め (新しいバーを追加)、両 key とも
    # 非 empty で succeeded — この時点では 1m も「健全」と記録される。
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, 0, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 0, 10, tzinfo=timezone.utc)
    both_healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    assert m.observe(t2, both_healthy) == "degraded"

    m.request_resume(datetime(2026, 9, 24, 10, 0, 15, tzinfo=timezone.utc),
                     acknowledge=False)

    # さらに次 tick: 1m は succeeded だが 0 本 (empty)。watermark 自体は
    # まだ 10:00:00 のままで停滞式 (10:00:00 + 2*1min + 30s = 10:02:30) を
    # 過ぎていない — stalled 式だけでは検出できないケース。
    t3 = datetime(2026, 9, 24, 10, 0, 20, tzinfo=timezone.utc)
    empty_recovery = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")], empty=[(PAIR, "1m")])
    state = m.observe(t3, empty_recovery)

    assert state == "degraded"
    rejected = [l for l in log.tail(50) if "data_resume_rejected" in l]
    assert len(rejected) == 1
    assert "watermark_unhealthy" in rejected[0]
    assert f"unconfirmed_keys={PAIR}:1m" in rejected[0]


def test_unprocessed_bars_activity_fires_once_per_change_while_degraded(tmp_path):
    """episode が開いている間、未処理建玉の内訳 (pair・推定足数) が前回と
    変わった tick でだけ `data_outage_unprocessed_bars` を書く — 同じ内訳が
    続く tick では再送せず、確定足が増えて bars が変わった tick では
    再度 1 回書く。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)

    def _lines():
        return [l for l in log.tail(50) if "data_outage_unprocessed_bars" in l]

    # 10:01:00 — 1m 失敗で degraded へ。gap_start(1m) = 09:59 (直前 tick で
    # succeeded だった最後の watermark)。OPEN 建玉はまだ無いので内訳は空
    # (activity は書かれない)。
    t1 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t1, failed) == "degraded"
    assert _lines() == []

    # OPEN 建玉を作る (gap_start より前に約定済み) — 未処理判定の下限は
    # next_expected_trading_time(09:59) = 10:00。
    orders.insert(
        conn, pair=PAIR, direction="long", entry_type="market", horizon="day",
        status="open", now=datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc),
        quantity=0.1, avg_fill_price=148.0,
        filled_at=datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc).isoformat())

    # 10:02:00 — 10:00 の 1m 足が確定 → 未処理建玉 1 件・~1 本。最初の
    # 非空内訳なので 1 回書く。
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 2, 0, tzinfo=timezone.utc)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    assert m.observe(t2, healthy) == "degraded"
    lines = _lines()
    assert len(lines) == 1
    assert "USDJPY: 1 positions / ~1 bars" in lines[0]

    # 10:02:30 — 確定足に変化なし (bars 内訳が前回と同じ) → 再送しない。
    t3 = datetime(2026, 9, 24, 10, 2, 30, tzinfo=timezone.utc)
    assert m.observe(t3, healthy) == "degraded"
    assert len(_lines()) == 1

    # 10:03:00 — 10:01 の 1m 足も確定 → bars が 1→2 に変化、再度 1 回書く。
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    t4 = datetime(2026, 9, 24, 10, 3, 0, tzinfo=timezone.utc)
    assert m.observe(t4, healthy) == "degraded"
    lines = _lines()
    assert len(lines) == 2
    assert "USDJPY: 1 positions / ~2 bars" in lines[1]


def test_unprocessed_bars_notification_resets_after_resume_acceptance(tmp_path):
    """resume 受理 (degraded → ready) の tick で通知メモ
    (`_last_unprocessed_by_pair`) が None に戻ること (白箱 pin)。加えて、
    その直後に新しい episode で未処理建玉の内訳が再び現れたときも
    `data_outage_unprocessed_bars` が出ることを不変条件として確認する
    (episode 2 の開始 tick 自体は gap_start 直後で未処理 0 本と計算される
    ため、この振る舞い自体はメモの解除有無によらず成立する — メモ解除の
    直接的な観測は白箱 pin 側で行う)。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)

    def _lines():
        return [l for l in log.tail(50) if "data_outage_unprocessed_bars" in l]

    orders.insert(
        conn, pair=PAIR, direction="buy", entry_type="market", horizon="swing",
        status="open", now=datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc),
        filled_at=datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc).isoformat())

    # episode 1: 1m 失敗 → degraded、gap_start(1m)=09:59。
    t1 = datetime(2026, 9, 24, 10, 1, 0, tzinfo=timezone.utc)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t1, failed) == "degraded"
    assert _lines() == []

    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 2, 0, tzinfo=timezone.utc)
    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    assert m.observe(t2, healthy) == "degraded"
    assert len(_lines()) == 1

    # --acknowledge で resume を受理させる (OPEN 建玉は残ったまま)。停滞式に
    # 触れないよう watermark(10:00) から 30 秒未満の近接 tick にする。
    m.request_resume(datetime(2026, 9, 24, 10, 2, 5, tzinfo=timezone.utc), acknowledge=True)
    t3 = datetime(2026, 9, 24, 10, 2, 20, tzinfo=timezone.utc)
    assert m.observe(t3, healthy) == "ready"
    assert len(_lines()) == 1  # 内訳不変の resume tick 自体では増えない
    assert m._last_unprocessed_by_pair is None

    # episode 2: 再び 1m 失敗 → 新 epoch。
    t4 = datetime(2026, 9, 24, 10, 3, 0, tzinfo=timezone.utc)
    failed2 = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    assert m.observe(t4, failed2) == "degraded"
    assert len(_lines()) == 1  # 確定足がまだ gap に追いついていない

    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    t5 = datetime(2026, 9, 24, 10, 4, 0, tzinfo=timezone.utc)
    assert m.observe(t5, healthy) == "degraded"
    lines = _lines()
    assert len(lines) == 2
    assert "USDJPY: 1 positions / ~1 bars" in lines[1]


def test_recovered_awaiting_resume_notification_survives_process_restart(tmp_path):
    """`datafeed_recovered_awaiting_resume` の一回性は DB 永続の
    `recovered_notified_epoch` で判定する — 同一 epoch・同一健全 report で
    プロセス (`OutageStateMachine` インスタンス) を作り直しても再送しない。
    新しい epoch (再度 failed → succeeded) では改めて 1 回出る。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")

    def _lines():
        return [l for l in log.tail(50) if "datafeed_recovered_awaiting_resume" in l]

    healthy = _report(succeeded=[(PAIR, "1m"), (PAIR, "1h")])
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])

    m1 = _machine(conn, activity=log, auto_resume_when_flat=False)
    t1 = datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc)
    assert m1.observe(t1, failed) == "degraded"

    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc))
    t2 = datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc)
    assert m1.observe(t2, healthy) == "degraded"
    assert len(_lines()) == 1

    # プロセス再起動を模して新インスタンスを同じ conn 上に作る。
    m2 = _machine(conn, activity=log, auto_resume_when_flat=False)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc))
    t3 = datetime(2026, 9, 24, 10, 8, tzinfo=timezone.utc)
    assert m2.observe(t3, healthy) == "degraded"
    assert len(_lines()) == 1  # 同じ epoch・健全 report では再送しない

    # OPEN/PENDING_FILL は無いので resume はそのまま受理される。
    m2.request_resume(datetime(2026, 9, 24, 10, 9, tzinfo=timezone.utc), acknowledge=False)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 10, tzinfo=timezone.utc))
    t4 = datetime(2026, 9, 24, 10, 12, tzinfo=timezone.utc)
    assert m2.observe(t4, healthy) == "ready"

    # 新しい epoch: 再度 failed → succeeded で改めて 1 回出る。
    t5 = datetime(2026, 9, 24, 10, 13, tzinfo=timezone.utc)
    assert m2.observe(t5, failed) == "degraded"
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 10, 17, tzinfo=timezone.utc))
    t6 = datetime(2026, 9, 24, 10, 19, tzinfo=timezone.utc)
    assert m2.observe(t6, healthy) == "degraded"
    assert len(_lines()) == 2


def test_confirmed_outage_activity_event_is_data_outage_degraded(tmp_path):
    """confirmed 昇格 (2 tick 連続) の activity event 名が
    `data_outage_degraded` であること (改名の pin、単独テスト)。"""
    conn = _db(tmp_path)
    _seed_bar(conn, PAIR, "1m", datetime(2026, 9, 24, 9, 59, tzinfo=timezone.utc))
    _seed_bar(conn, PAIR, "1h", datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
    log = ActivityLog(tmp_path / "activity.log")
    m = _machine(conn, activity=log)
    failed = _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])
    m.observe(datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc), failed)
    m.observe(datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc), failed)
    events = [l for l in log.tail(50) if "\tdata_outage_degraded\t" in l]
    assert len(events) == 1


def _auto_machine(conn, *, activity=None, **kwargs):
    return OutageStateMachine(
        conn, hard_keys=HARD_KEYS, interval_widths=WIDTHS, grace=GRACE,
        storage_source="mt5-live", activity=activity, **kwargs)


def _degrade(machine, now):
    assert machine.observe(now, _report(
        failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])) == "degraded"


def test_flat_episode_auto_resumes_after_three_healthy_ticks(tmp_path):
    conn = _db(tmp_path)
    log = ActivityLog(tmp_path / "activity.log")
    machine = _auto_machine(conn, activity=log)
    _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    healthy = _report(succeeded=HARD_KEYS)
    for minute in (2, 3):
        assert machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy) == "degraded"
    assert machine.observe(datetime(2026, 9, 24, 10, 4, tzinfo=timezone.utc), healthy) == "ready"
    lines = log.tail(50)
    assert len([line for line in lines if "datafeed_recovered_auto" in line]) == 1
    assert not any("datafeed_recovered_awaiting_resume" in line for line in lines)


def test_auto_resume_streak_resets_on_empty_tick(tmp_path):
    conn = _db(tmp_path)
    machine = _auto_machine(conn)
    _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    healthy = _report(succeeded=HARD_KEYS)
    for minute in (2, 3):
        machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy)
    empty = _report(succeeded=HARD_KEYS, empty=[(PAIR, "1m")])
    assert machine.observe(datetime(2026, 9, 24, 10, 4, tzinfo=timezone.utc), empty) == "degraded"
    for minute in (5, 6):
        assert machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy) == "degraded"
    assert machine.observe(datetime(2026, 9, 24, 10, 7, tzinfo=timezone.utc), healthy) == "ready"


def test_open_or_pending_order_prevents_auto_resume(tmp_path):
    for status in ("open", "pending_fill"):
        conn = _db(tmp_path / status)
        log = ActivityLog(tmp_path / status / "activity.log")
        machine = _auto_machine(conn, activity=log)
        _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
        orders.insert(conn, pair=PAIR, direction="buy", entry_type="market",
                      horizon="swing", status=status,
                      now=datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
        healthy = _report(succeeded=HARD_KEYS)
        for minute in (2, 3, 4):
            assert machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy) == "degraded"
        assert len([line for line in log.tail(50) if "datafeed_recovered_awaiting_resume" in line]) == 1


def test_auto_resume_uses_last_success_for_not_attempted_key(tmp_path):
    conn = _db(tmp_path)
    keys = frozenset({(PAIR, "1m"), (PAIR, "15m")})
    machine = OutageStateMachine(conn, hard_keys=keys,
        interval_widths={"1m": timedelta(minutes=1), "15m": timedelta(minutes=15)},
        grace=GRACE, storage_source="mt5-live")
    assert machine.observe(datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc),
        _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "15m")])) == "degraded"
    assert machine.observe(datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc),
        _report(succeeded=keys)) == "degraded"
    one_minute = _report(succeeded=[(PAIR, "1m")])
    assert machine.observe(datetime(2026, 9, 24, 10, 3, tzinfo=timezone.utc), one_minute) == "degraded"
    assert machine.observe(datetime(2026, 9, 24, 10, 4, tzinfo=timezone.utc), one_minute) == "ready"


def test_auto_resume_streak_resets_after_process_restart(tmp_path):
    conn = _db(tmp_path)
    machine = _auto_machine(conn)
    _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    healthy = _report(succeeded=HARD_KEYS)
    for minute in (2, 3):
        machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy)
    restarted = _auto_machine(conn)
    for minute in (4, 5):
        assert restarted.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy) == "degraded"
    assert restarted.observe(datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc), healthy) == "ready"


def test_resume_request_on_auto_resume_tick_is_consumed_without_second_ready(tmp_path):
    conn = _db(tmp_path)
    log = ActivityLog(tmp_path / "activity.log")
    machine = _auto_machine(conn, activity=log)
    _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    healthy = _report(succeeded=HARD_KEYS)
    for minute in (2, 3):
        machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy)
    machine.request_resume(datetime(2026, 9, 24, 10, 4, tzinfo=timezone.utc))
    assert machine.observe(datetime(2026, 9, 24, 10, 4, tzinfo=timezone.utc), healthy) == "ready"
    lines = log.tail(50)
    assert len([line for line in lines if "datafeed_recovered_auto" in line]) == 1
    assert not any("data_resume_accepted" in line for line in lines)


def test_auto_resume_flap_opens_next_epoch(tmp_path):
    conn = _db(tmp_path)
    log = ActivityLog(tmp_path / "activity.log")
    machine = _auto_machine(conn, activity=log)
    _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
    healthy = _report(succeeded=HARD_KEYS)
    for minute in (2, 3, 4):
        machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy)
    assert machine.observe(datetime(2026, 9, 24, 10, 5, tzinfo=timezone.utc),
        _report(failed=[((PAIR, "1m"), "ConnectError")], succeeded=[(PAIR, "1h")])) == "degraded"
    assert machine.status()["epoch"] == 2
    assert len([line for line in log.tail(50) if "datafeed_degraded" in line]) == 2
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap WHERE epoch=2").fetchone()[0] == 2


def test_auto_resume_can_be_disabled_or_blocked_by_human_confirmation(tmp_path):
    for kwargs in ({"auto_resume_when_flat": False}, {}):
        conn = _db(tmp_path / str(kwargs))
        log = ActivityLog(tmp_path / str(kwargs) / "activity.log")
        machine = _auto_machine(conn, activity=log, **kwargs)
        _degrade(machine, datetime(2026, 9, 24, 10, 1, tzinfo=timezone.utc))
        if not kwargs:
            conn.execute("UPDATE datafeed_outage_state SET pending_human_confirmation=1 WHERE id=1")
            conn.commit()
        healthy = _report(succeeded=HARD_KEYS)
        for minute in (2, 3, 4):
            assert machine.observe(datetime(2026, 9, 24, 10, minute, tzinfo=timezone.utc), healthy) == "degraded"
        awaiting = [line for line in log.tail(50) if "datafeed_recovered_awaiting_resume" in line]
        assert len(awaiting) == (1 if kwargs else 0)

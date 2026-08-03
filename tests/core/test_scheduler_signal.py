"""Scheduler の signal 起動テスト (プラン 7 Task 8)。

`Env`/`WED`/`SAT`/`SETTINGS` は `tests/core/test_scheduler.py` のものを再利用
する (既存の Env は on_signal_maintenance/signal_due_fn を持たないので、
各テストで `env.sched.on_signal_maintenance`/`env.sched.signal_due_fn` を
直接差し替える — 既存テストの `env.executor.broker.equity = ...` と同じ
流儀)。

`_signal_due_fn` は service.py の実配線 (D2 AND pending_exists AND
signals_rate_ok) を模した combinator — 実 store 関数 (signals/missions/
orders) をそのまま呼ぶので、レート制限・D2 の意味論は store 層のロジック
そのものを検証している。
"""
from __future__ import annotations

from datetime import timedelta

from agentic_fx.core.contracts import Bar
from agentic_fx.store import missions, orders, signals

from tests.core.test_scheduler import SAT, SETTINGS, WED, Env


def _signal_due_fn(conn, settings):
    """service.py の signal_due_fn 配線を模した combinator (D2 AND
    pending_exists AND signals_rate_ok)。"""
    def fn(now):
        if not orders.list_by_status(conn, "open", "pending_fill"):
            return False
        if not signals.pending_exists(conn):
            return False
        return missions.signals_rate_ok(conn, now, settings)
    return fn


def _open_position(env, *, pair="USDJPY"):
    return orders.insert(
        env.conn, pair=pair, direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)


def _add_pending_signal(env, *, bar_ts):
    return signals.add(
        env.conn, plugin="p", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=bar_ts.isoformat(), kind="signal",
        payload={"direction": "long"}, now=bar_ts)


# ---------------------------------------------------------------------
# ① pending + ポジションありで "signal"
# ---------------------------------------------------------------------
def test_signal_fires_when_pending_and_position_exist(tmp_path):
    env = Env(tmp_path)
    env.sched._last_cron_trade = WED  # cron を非該当にして signal 経路だけ見る
    _open_position(env)
    _add_pending_signal(env, bar_ts=WED - timedelta(hours=1))
    env.sched.signal_due_fn = _signal_due_fn(env.conn, SETTINGS)

    env.sched.tick(WED + timedelta(minutes=5))
    assert env.trade_reasons == ["signal"]


# ---------------------------------------------------------------------
# ② ポジション・pending_fill ゼロで起動しない (D2)
# ---------------------------------------------------------------------
def test_signal_does_not_fire_without_open_or_pending_position(tmp_path):
    env = Env(tmp_path)
    env.sched._last_cron_trade = WED
    _add_pending_signal(env, bar_ts=WED - timedelta(hours=1))  # pending はある
    env.sched.signal_due_fn = _signal_due_fn(env.conn, SETTINGS)

    env.sched.tick(WED + timedelta(minutes=5))
    assert env.trade_reasons == []  # D2 (ポジション皆無) で起動しない


# ---------------------------------------------------------------------
# ③ シグナル起動後も cron 締切不変 (2 tick)
# ---------------------------------------------------------------------
def test_signal_dispatch_does_not_move_cron_deadline(tmp_path):
    env = Env(tmp_path)
    env.sched.signal_due_fn = lambda now: True

    env.sched.tick(WED)  # 初回は cron (締切未設定)
    assert env.trade_reasons == ["cron"]
    cron_deadline = env.sched._last_cron_trade
    assert cron_deadline == WED

    env.sched.tick(WED + timedelta(minutes=10))  # cron 未到来 → signal
    assert env.trade_reasons == ["cron", "signal"]
    assert env.sched._last_cron_trade == cron_deadline  # 締切は動かない

    env.sched.tick(WED + timedelta(minutes=20))  # もう 1 tick — なお不変
    assert env.trade_reasons == ["cron", "signal", "signal"]
    assert env.sched._last_cron_trade == cron_deadline


# ---------------------------------------------------------------------
# ④ 10 分以内・日次 12 回超過は起動しない・ロールオーバーでリセット
# ---------------------------------------------------------------------
def test_signal_rate_limit_min_interval(tmp_path):
    assert SETTINGS.plugin.signal_min_interval_min == 10
    env = Env(tmp_path)
    env.sched._last_cron_trade = WED
    _open_position(env)
    _add_pending_signal(env, bar_ts=WED - timedelta(hours=1))
    env.sched.signal_due_fn = _signal_due_fn(env.conn, SETTINGS)

    last_signal_at = WED
    missions.start(env.conn, "trade", "local", "m", last_signal_at,
                   trigger="signal")

    env.sched.tick(last_signal_at + timedelta(minutes=5))  # 5分 < 10分
    assert env.trade_reasons == []

    env.sched.tick(last_signal_at + timedelta(minutes=11))  # 11分 >= 10分
    assert env.trade_reasons == ["signal"]


def test_skipped_signal_mission_counts_toward_min_interval(tmp_path):
    """claim 失敗で "skipped" 終端した Mission (trigger の暫定値 "signal"
    のまま、`:` 以降が確定していない) も `signals_rate_ok` の LIKE
    'signal%' にヒットし、レート制限に算入される (docstring 逐語の意図:
    「シグナル起動の試行そのもの」を数える)。"""
    env = Env(tmp_path)
    env.sched._last_cron_trade = WED
    _open_position(env)
    _add_pending_signal(env, bar_ts=WED - timedelta(hours=1))
    env.sched.signal_due_fn = _signal_due_fn(env.conn, SETTINGS)

    # 暫定値 "signal" のまま skipped 終端した Mission 行 (claim 失敗を模す)
    missions.start(env.conn, "trade", "local", "m", WED, trigger="signal")
    missions.finish(env.conn, 1, "skipped", None, [], WED)

    env.sched.tick(WED + timedelta(minutes=5))  # 最短間隔 (10分) 未満
    assert env.trade_reasons == []


def test_signal_daily_max_and_rollover_reset(tmp_path):
    assert SETTINGS.plugin.signal_daily_max == 12
    env = Env(tmp_path)
    _open_position(env)
    _add_pending_signal(env, bar_ts=WED - timedelta(hours=1))
    env.sched.signal_due_fn = _signal_due_fn(env.conn, SETTINGS)

    from agentic_fx.core import market_hours
    day_start = market_hours.trading_day_start(WED)
    # signal_daily_max (12) 件を当日枠に、最短間隔 (11分おき) を満たしつつ挿入
    times = [day_start + timedelta(minutes=11 * i) for i in range(12)]
    for t in times:
        missions.start(env.conn, "trade", "local", "m", t, trigger="signal")
    last = times[-1]

    env.sched._last_cron_trade = last - timedelta(minutes=5)  # cron を抑制
    env.sched.tick(last + timedelta(minutes=11))  # 最短間隔は満たすが日次上限超過
    assert env.trade_reasons == []

    # 翌取引日 (trading_day_start のロールオーバー後) はカウンタがリセットされる
    next_day = day_start + timedelta(days=1, hours=1)
    env.sched._last_cron_trade = next_day - timedelta(minutes=5)  # cron を抑制
    env.sched.tick(next_day)
    assert env.trade_reasons == ["signal"]


# ---------------------------------------------------------------------
# ⑤ on_signal_maintenance が開場中のみ呼ばれ、例外が tick を殺さない (fail-open)
# ---------------------------------------------------------------------
def test_on_signal_maintenance_runs_only_when_open_and_fails_open(tmp_path):
    calls = []

    def maint(now):
        calls.append(now)
        raise RuntimeError("maintenance boom")

    env = Env(tmp_path)
    env.sched.on_signal_maintenance = maint

    env.sched.tick(SAT)  # 閉場中は呼ばれない
    assert calls == []

    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # 開場中は呼ばれる
    assert len(calls) == 1
    # 例外が資金保護 (約定処理) を止めていないこと
    assert orders.get(env.conn, oid)["status"] == "open"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "signal_maintenance_cycle_error" in log


def test_on_signal_maintenance_default_none_is_a_noop(tmp_path):
    """既定 (None) は機能無効 — 既存テスト互換の直接ピン。"""
    env = Env(tmp_path)
    assert env.sched.on_signal_maintenance is None
    env.sched.tick(WED)  # 例外を出さず通常どおり動く
    assert env.trade_calls == 1


# ---------------------------------------------------------------------
# ⑳ 古い pending (freshness 超過) が maintenance (expire_stale) で
#    起動判定より前に abandoned になる (D4)
# ---------------------------------------------------------------------
def test_stale_pending_signal_is_abandoned_before_due_check(tmp_path):
    env = Env(tmp_path)
    env.sched._last_cron_trade = WED - timedelta(minutes=5)  # cron を抑制
    _open_position(env)  # D2 を満たす
    stale_bar_ts = WED - timedelta(hours=10)  # 鮮度窓 (既定 2h) を大きく超える
    sid = _add_pending_signal(env, bar_ts=stale_bar_ts)

    def on_signal_maintenance(now):
        signals.expire_stale(
            env.conn, now=now,
            freshness_bars=SETTINGS.plugin.signal_freshness_bars)

    env.sched.on_signal_maintenance = on_signal_maintenance
    env.sched.signal_due_fn = _signal_due_fn(env.conn, SETTINGS)

    env.sched.tick(WED)  # maintenance (stale 掃除) → 同 tick 内の due 判定
    row = env.conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "abandoned"
    assert env.trade_reasons == []  # pending が無くなったので signal 起動しない


# ---------------------------------------------------------------------
# fix round 1 F5 (codex): maintenance 例外 + activity.write 故障の二重
# 故障でも tick は完走する (「except ハンドラが故障源を共有する」パターン
# — maintenance は _process_limit_fills/_process_exits (資金保護) より前)
# ---------------------------------------------------------------------
def test_signal_maintenance_and_activity_write_double_failure_does_not_kill_tick(
        tmp_path):
    env = Env(tmp_path)

    def maint(now):
        raise RuntimeError("maintenance boom")

    env.sched.on_signal_maintenance = maint
    # activity.write を全面的に壊すと、tick 内の他の (この F5 とは無関係な)
    # write 呼び出し (limit_filled 等) まで巻き込んで tick が別の理由で
    # 落ちてしまう。二重故障を `_run_data_hook` の except 経路だけに絞る。
    real_write = env.sched.activity.write

    def flaky_write(category, event, *args, **kwargs):
        if event == "signal_maintenance_cycle_error":
            raise RuntimeError("activity boom")
        return real_write(category, event, *args, **kwargs)

    env.sched.activity.write = flaky_write

    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # 二重故障でも例外が漏れない

    assert orders.get(env.conn, oid)["status"] == "open"  # 約定処理へ到達した


# ---------------------------------------------------------------------
# signal_due_fn 自体の例外は「起動しない」に倒す (fail-open だが tick は
# 継続する — sonnet Minor 指摘)
# ---------------------------------------------------------------------
def test_signal_due_fn_exception_is_fail_open(tmp_path):
    env = Env(tmp_path)
    env.sched._last_cron_trade = WED  # cron を抑制し signal 経路だけ見る

    def boom(now):
        raise RuntimeError("signal_due_fn boom")

    env.sched.signal_due_fn = boom

    env.sched.tick(WED + timedelta(minutes=5))  # 例外が漏れない

    assert env.trade_reasons == []  # 起動しない
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "signal_due_check_error" in log

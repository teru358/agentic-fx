# [decision-timeframe-config] B-2 実装プラン 段 a + 段 b (v1.1) — 段 b (時刻モデル)

共通の Global Constraints / プラン規約 / 呼び出し元の全数表は `docs/superpowers/plans/2026-09-26-decision-timeframe-b2-ab.md` (以下「段 a ファイル」) を正とし、ここでは繰り返さない。**段 a が main に入ってから着手する。** 行番号は `43c6042` 基準に段 a の変更を当てた後の現物で `grep -n` し直して当てる (内容の一致で当てる)。

設計書: `docs/superpowers/specs/2026-09-26-decision-timeframe-b2-design.md` §3.2 (閉場境界・cursor 遷移表)、§3.5 (provenance・snapshot・単一 transaction)、§4 IV-2/3/10/11、§5 AC-B2-05〜12・21 (a)(b)・22〜25、§9-5、§10。

## 段 b の追加規律 (段 a ファイルの Global Constraints に加えて)

- **transaction 境界**: `cron_cursor` の upsert は `with conn:` (例外時に自動 ROLLBACK、`store/missions.py` の既存規約と同じ) の中で 1 文だけ。`missions` 行と `mission_decision_bars` 行は `missions.start(..., commit=False)` → INSERT → `conn.commit()` の 1 transaction で確定し、例外時は `conn.rollback()` してから再送出する。どちらも SQL 例外の後に `conn_core` の `in_transaction` を残さない (IV-11)。
- **core_lock**: cursor の永続化・復元は `Scheduler` の中 (`tick` は `service._scheduler_tick_once` の `with app.core_lock:` の内側、`__init__` は `build_app` の単一スレッド初期化中) でだけ行う。`mission_decision_bars` の書込みは `TradeLoop` prepare の `with self._core_lock:` の内側だけ。新しいスレッドから `conn_core` を触らない。
- **`now` は tick の引数だけ**: `session_start(now)`・`cron_cursor.updated_at`・activity の summary は `tick(now)` の `now` を使う。
- **資金保護の順序**: 永続化の再試行は `tick` の `finally:` の `_run_hooks(now)` の直後 (資金保護は `try:` 本体で既に済んでいる)。session 判定と受理時の永続化は `finally` の後の受理ブロック。どちらも `_force_close_day` / `_process_exits` より前に置かない (AC-B2-24)。
- **§9-5 実測** (`tmp/design-b2/measurements.md` #5): `cron_cursor` の upsert は 1 pair で p99 0.018 ms/tick、5 pair で p99 0.028 ms/tick。core_lock 保持時間への影響は無視でき、設計変更は不要。

## File Structure (段 b)

| ファイル | 変更 | task |
|---|---|---|
| `src/agentic_fx/core/market_hours.py` | `session_start(now)` 新設 | Tb1 |
| `src/agentic_fx/core/scheduler.py` | 前セッション足 skip (Tb1)。cursor の復元・永続化・dirty 再試行・未来値 hold/自動復帰 (Tb2)。受理時の snapshot 受渡し (Tb3) | Tb1〜Tb3 |
| `src/agentic_fx/store/db.py` | `cron_cursor`・`mission_decision_bars` の DDL を `_SCHEMA` に追記、`TABLE_NAMES` に 2 名 | Tb2 |
| `src/agentic_fx/store/cron_cursor.py` | **新規**: `upsert` / `load_all` | Tb2 |
| `src/agentic_fx/store/mission_decision_bars.py` | **新規**: `insert_many` (commit しない) / `for_mission` | Tb2 |
| `src/agentic_fx/core/supervisor.py` | `_dispatch` が `decision_bars` を trade_fn へ転送 | Tb3 |
| `src/agentic_fx/service.py` | `on_trade_mission(trigger, *, decision_bars)`・`_trade_fn` の転送 | Tb3 |
| `src/agentic_fx/loops/trade_loop.py` | `run_once`/`_run_once_impl` に `decision_bars`、cron mission の単一 transaction | Tb3 |
| `tests/core/test_market_hours.py` | **追記** (`session_start`) | Tb1 |
| `tests/core/test_scheduler_decision_timeframe.py` | **追記** (AC-B2-05〜08・24b・21b / 09〜11・22・25 / snapshot) | Tb1〜Tb3 |
| `tests/core/test_scheduler.py` | `test_open_restart_runs_latest_watermark_once` (`:306-315`) を書換え (AC-B2-21 (a)) | Tb2 |
| `tests/store/test_cron_cursor.py` | **新規** | Tb2 |
| `tests/store/test_db.py:10-17,20` / `tests/store/test_db_migrations.py:31-41` | `TABLE_NAMES` pin 2 本に 2 名を追加 | Tb2 |
| `tests/loops/test_trade_loop.py` | **追記** (AC-B2-12・23) | Tb3 |
| `tests/core/test_supervisor.py` / `tests/test_service_app.py` | **追記** (転送と end-to-end の provenance) | Tb3 |

**cursor の復元位置**: `Scheduler.__init__` の末尾で `cron_cursor` を読む。`build_app` は `init_db(conn_core)` (`service.py:761`) の後に `Scheduler(...)` (`:1173`) を構築するので、service.py 側に復元のための配線は要らない (設計書 §10 の「service.py: cursor 復元の配線」は本プランでは空になる。spec との食い違い #10)。

## 受入条件と task の対応 (段 b)

| AC | task | テスト名 (予定) |
|---|---|---|
| `session_start` の定義 | Tb1 | `test_session_start_is_last_closed_to_open_transition` (6 例) |
| AC-B2-05 | Tb1 | `test_friday_last_bar_committed_at_sunday_open_is_skipped_not_fired` |
| AC-B2-06 | Tb1 | `test_holiday_not_joined_to_weekend_skips_pre_holiday_bar` |
| AC-B2-07 | Tb1 | `test_new_year_joined_to_weekend_skips_until_sunday_open` |
| AC-B2-08 | Tb1 | `test_christmas_friday_joined_to_weekend_is_not_a_session_start` |
| AC-B2-21 (b) | Tb1 | `test_default_1h_friday_last_bar_is_skipped_at_sunday_open` |
| AC-B2-24 (b) | Tb1 | `test_session_start_exception_leaves_day_close_and_sl_already_done` |
| AC-B2-09 | Tb2 | `test_restart_restores_cursor_and_does_not_rejudge_same_bar` |
| AC-B2-10a / 10c / 10d(前半) | Tb2 | `test_cursor_write_failure_is_retried_in_closed_ticks_once_per_tick` |
| AC-B2-10b | Tb2 | `test_cursor_write_failure_is_retried_while_outage_is_degraded` |
| AC-B2-10c (同一 tick の二重記録なし) | Tb2 | `test_cursor_write_failure_is_logged_once_per_tick_even_with_retry_and_accept` |
| AC-B2-10d (後半、at-least-once) | Tb2 | `test_unpersisted_cursor_is_rejudged_after_restart_at_least_once` |
| AC-B2-11 / AC-B2-25 | Tb2 | `test_future_cursor_holds_only_that_pair_and_resumes_when_w_catches_up` |
| AC-B2-22 | Tb2 | `test_restored_cursor_without_observed_w_is_left_unverified` |
| AC-B2-21 (a) | Tb2 | `test_open_restart_restores_cursor_and_does_not_rerun_latest_watermark` (既存の書換え) |
| store / migration / IV-11 | Tb2 | `tests/store/test_cron_cursor.py` の 4 本 + TABLE_NAMES pin 2 本 |
| AC-B2-12 | Tb3 | `test_cron_passes_only_advanced_pairs_as_immutable_snapshot` / `test_cron_mission_records_decision_bars_per_pair_and_order_joins_back` / `test_cron_tick_records_decision_bar_provenance_end_to_end` |
| AC-B2-23 | Tb3 | `test_decision_bars_insert_failure_rolls_back_mission_and_leaves_mid_none` |
| snapshot 転送 | Tb3 | `test_trade_job_forwards_decision_bars_only_when_given` |

## task 依存図 (段 b)

```text
Tb1 (session_start + 前セッション足 skip) → Tb2 (cron_cursor/mission_decision_bars 表 + 永続・復元・dirty・hold) → Tb3 (snapshot + 単一 transaction) → 段 b 全体 green
```

Tb1 と Tb2 はどちらも `Scheduler._trade_mission_due` を書き換えるので直列。Tb2 の store 部分 (DDL・`cron_cursor.py`・`mission_decision_bars.py`・`tests/store/*`) だけは Tb1 と並列に起こしてよい (scheduler への組込みは Tb1 の後)。

---

## Tb1: `session_start` と前セッション足の skip

### Step 1-a: テストを置いて red を確認する

- [ ] `tests/core/test_market_hours.py` に追記 (import に `import pytest`・`from datetime import datetime, timezone`・**`from agentic_fx.core import market_hours`** が無ければ足す。既存の import 節は `from agentic_fx.core.market_hours import (is_friday_after, is_market_open, next_expected_trading_time, next_rollover, trading_day_start)` のように個々の関数しか import していないため、モジュール属性として呼ぶ `market_hours.session_start(now)` を使うにはこの行を別途足す必要がある — 足し忘れると実装後も `NameError: name 'market_hours' is not defined` のまま red が残る (実測で判明、2026-09-26 着手前検証)):

```python
_U = timezone.utc


@pytest.mark.parametrize(("now", "expected"), [
    # 週中 → 直前の日曜 21:00
    (datetime(2026, 9, 24, 12, 0, tzinfo=_U), datetime(2026, 9, 20, 21, 0, tzinfo=_U)),
    # 開場の瞬間はその境界自身
    (datetime(2026, 9, 27, 21, 0, tzinfo=_U), datetime(2026, 9, 27, 21, 0, tzinfo=_U)),
    # 金曜 20:59:59 はまだ同じセッション
    (datetime(2026, 9, 25, 20, 59, 59, tzinfo=_U), datetime(2026, 9, 20, 21, 0, tzinfo=_U)),
    # 祝日 (2025-12-25 木) が週末と連結しない: 祝日明けの 21:00
    (datetime(2025, 12, 26, 12, 0, tzinfo=_U), datetime(2025, 12, 25, 21, 0, tzinfo=_U)),
    # 2026-12-25 (金) は週末と連結: 金 21:00 ではなく日 27 21:00
    (datetime(2026, 12, 28, 12, 0, tzinfo=_U), datetime(2026, 12, 27, 21, 0, tzinfo=_U)),
    # 2027-01-01 (金) も連結: 日 2027-01-03 21:00
    (datetime(2027, 1, 4, 12, 0, tzinfo=_U), datetime(2027, 1, 3, 21, 0, tzinfo=_U)),
])
def test_session_start_is_last_closed_to_open_transition(now, expected):
    assert market_hours.session_start(now) == expected
```

- [ ] `tests/core/test_scheduler_decision_timeframe.py` に追記 (import に `from agentic_fx.core import market_hours`、`from datetime import timedelta` を足す):

```python
FRI = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)   # 金曜
SUN = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)   # 日曜 開場
KEY = ("USDJPY", "15m")


def _dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def test_friday_last_bar_committed_at_sunday_open_is_skipped_not_fired(tmp_path):
    env, slot = _env(tmp_path, FRI)
    _seed_bar(env, _at(FRI, 20, 15))
    _tick(env, slot, _at(FRI, 20, 30, 30))
    _seed_bar(env, _at(FRI, 20, 30))
    _tick(env, slot, _at(FRI, 20, 45, 30))
    assert len(slot.accepted_at) == 2
    _tick(env, slot, _at(FRI, 20, 59, 59))
    assert env.sched._was_open is True and len(slot.accepted_at) == 2
    _tick(env, slot, _at(FRI, 21, 0, 0))
    assert env.sched._was_open is False
    _tick(env, slot, _at(FRI, 21, 0, 30))
    assert len(slot.accepted_at) == 2
    # 金 20:45 足 (確定 21:00:30) は閉場で取り込まれず、日曜の開場 tick で commit される
    _seed_bar(env, _at(FRI, 20, 45))
    _tick(env, slot, _at(SUN, 21, 0, 30))
    assert len(slot.accepted_at) == 2
    assert env.sched._cron_watermarks[KEY] == _at(FRI, 20, 45)
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-09-25T20:45 session_start=2026-09-27T21:00"]
    _seed_bar(env, _at(SUN, 21, 0))
    _tick(env, slot, _at(SUN, 21, 15, 30))
    assert slot.accepted_at[-1] == _at(SUN, 21, 15, 30) and len(slot.accepted_at) == 3
    assert env.sched._cron_watermarks[KEY] == _at(SUN, 21, 0)


def test_holiday_not_joined_to_weekend_skips_pre_holiday_bar(tmp_path):
    base = _dt(2025, 12, 24, 20, 0)                        # 水曜
    env, slot = _env(tmp_path, base)
    _seed_bar(env, _dt(2025, 12, 24, 20, 30))
    _tick(env, slot, _dt(2025, 12, 24, 20, 45, 30))
    assert len(slot.accepted_at) == 1
    _tick(env, slot, _dt(2025, 12, 24, 21, 0, 30))         # 取引日 12/25 (祝日) へ
    _tick(env, slot, _dt(2025, 12, 25, 12, 0))
    assert env.sched._was_open is False
    _seed_bar(env, _dt(2025, 12, 24, 20, 45))
    _tick(env, slot, _dt(2025, 12, 25, 21, 0, 30))
    assert env.sched._was_open is True and len(slot.accepted_at) == 1
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2025-12-24T20:45 session_start=2025-12-25T21:00"]
    _seed_bar(env, _dt(2025, 12, 25, 21, 0))
    _tick(env, slot, _dt(2025, 12, 25, 21, 15, 30))
    assert len(slot.accepted_at) == 2
    assert env.sched._cron_watermarks[KEY] == _dt(2025, 12, 25, 21, 0)


def test_new_year_joined_to_weekend_skips_until_sunday_open(tmp_path):
    base = _dt(2026, 12, 31, 20, 0)                        # 木曜
    env, slot = _env(tmp_path, base)
    _seed_bar(env, _dt(2026, 12, 31, 20, 30))
    _tick(env, slot, _dt(2026, 12, 31, 20, 45, 30))
    assert env.sched._cron_watermarks[KEY] == _dt(2026, 12, 31, 20, 30)
    for closed in (_dt(2026, 12, 31, 21, 0, 30), _dt(2027, 1, 1, 21, 0, 30),
                   _dt(2027, 1, 2, 12, 0)):
        _tick(env, slot, closed)
        assert env.sched._was_open is False
    assert len(slot.accepted_at) == 1
    _seed_bar(env, _dt(2026, 12, 31, 20, 45))
    _tick(env, slot, _dt(2027, 1, 3, 21, 0, 30))
    assert len(slot.accepted_at) == 1
    assert env.sched._cron_watermarks[KEY] == _dt(2026, 12, 31, 20, 45)
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-12-31T20:45 session_start=2027-01-03T21:00"]
    _seed_bar(env, _dt(2027, 1, 3, 21, 0))
    _tick(env, slot, _dt(2027, 1, 3, 21, 15, 30))
    assert len(slot.accepted_at) == 2


def test_christmas_friday_joined_to_weekend_is_not_a_session_start(tmp_path):
    base = _dt(2026, 12, 24, 20, 0)                        # 木曜
    env, slot = _env(tmp_path, base)
    _seed_bar(env, _dt(2026, 12, 24, 20, 30))
    _tick(env, slot, _dt(2026, 12, 24, 20, 45, 30))
    for closed in (_dt(2026, 12, 24, 21, 0, 30), _dt(2026, 12, 25, 21, 0, 30),
                   _dt(2026, 12, 26, 12, 0)):
        _tick(env, slot, closed)
        assert env.sched._was_open is False
    assert len(slot.accepted_at) == 1
    _seed_bar(env, _dt(2026, 12, 24, 20, 45))
    _tick(env, slot, _dt(2026, 12, 27, 21, 0, 30))
    assert len(slot.accepted_at) == 1
    assert env.sched._cron_watermarks[KEY] == _dt(2026, 12, 24, 20, 45)
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-12-24T20:45 session_start=2026-12-27T21:00"]
    _seed_bar(env, _dt(2026, 12, 27, 21, 0))
    _tick(env, slot, _dt(2026, 12, 27, 21, 15, 30))
    assert len(slot.accepted_at) == 2


def test_default_1h_friday_last_bar_is_skipped_at_sunday_open(tmp_path):
    """既定 (1h) でも意図的な差分 (b): 日曜開場 tick に金曜最終足で起動しない。"""
    env = Env(tmp_path, base=_at(FRI, 19, 0), seed_cron_bar=False)
    _seed_decision_bar(env, _at(FRI, 19, 0))
    env.sched.tick(_at(FRI, 20, 0, 30))
    assert env.trade_reasons == ["cron"]
    env.sched.tick(_at(FRI, 21, 0, 30))
    _seed_decision_bar(env, _at(FRI, 20, 0))              # 確定 21:00:30 = 閉場後
    env.sched.tick(_at(SUN, 21, 0, 30))
    assert env.trade_reasons == ["cron"]
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 1h bar=2026-09-25T20:00 session_start=2026-09-27T21:00"]
    _seed_decision_bar(env, SUN)
    env.sched.tick(SUN + timedelta(hours=1, seconds=30))
    assert env.trade_reasons == ["cron", "cron"]


def test_session_start_exception_leaves_day_close_and_sl_already_done(
        tmp_path, monkeypatch):
    env = Env(tmp_path, seed_cron_bar=False)
    day_oid = env.place_limit()
    swing_oid = orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED.replace(minute=1))
    near_close = WED.replace(hour=20, minute=57)
    _seed_decision_bar(env, WED.replace(hour=19))
    env.bars["USDJPY"] = Bar("USDJPY", "1m", near_close, 147.70, 147.75,
                             147.50, 147.55, 100)

    def boom(_now):
        raise RuntimeError("injected session_start failure")

    monkeypatch.setattr(market_hours, "session_start", boom)
    with pytest.raises(RuntimeError, match="injected session_start failure"):
        env.sched.tick(near_close)
    assert env.trade_reasons == []
    day = orders.get(env.conn, day_oid)
    assert (day["status"], day["close_reason"]) == ("closed", "day_rollover")
    swing = orders.get(env.conn, swing_oid)
    assert (swing["status"], swing["close_reason"]) == ("closed", "sl")
```

- [ ] **Critical (実測で判明、2026-09-26 着手前検証)**: 上記の import 追記を忘れると `test_session_start_is_last_closed_to_open_transition` の 6 例は実装後も `NameError: name 'market_hours' is not defined` のまま red が残る。import 節に `from agentic_fx.core import market_hours` を必ず足すこと (上記 Step 1-a の指示に含めた)。
- [ ] red を確認する: `uv run pytest -q tests/core/test_market_hours.py tests/core/test_scheduler_decision_timeframe.py`
      期待: `session_start` 6 例は import 追記前提で `AttributeError: module 'agentic_fx.core.market_hours' has no attribute 'session_start'` (import を忘れると `NameError` のまま — 上記 Critical)。週末・祝日 4 本は開場 tick で起動して `assert 3 == 2`・`assert (True is True and 2 == 1)`・`assert 2 == 1` (×2) で落ち、1h 版は `assert ['cron', 'cron'] == ['cron']` で落ちる。`test_session_start_exception_...` は `AttributeError: <module 'agentic_fx.core.market_hours' ...> has no attribute 'session_start'` (`monkeypatch.setattr` の対象属性が無い)。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): import 追記前は上記 6 例が `NameError` になったが、それ以外は期待どおり。**

### Step 1-b: 実装を転写する

- [ ] `src/agentic_fx/core/market_hours.py`: `next_rollover` の後に追加:

```python
_SESSION_SEARCH_MAX_DAYS = 14


def session_start(now: datetime) -> datetime:
    """直近の「閉場→開場」遷移の時刻 (21:00 UTC 境界) を返す。

    開閉の状態は 21:00 UTC の rollover 境界でしか変わらないので、直近の
    境界から 1 日ずつ遡り、その境界で開場かつ 1 分前が閉場の最初の境界を
    返す。曜日の固定パターンではないので、祝日と週末が連結した閉場
    (2026-12-25 金・2027-01-01 金) の途中の金曜 21:00 を開場と誤らない。
    """
    boundary = trading_day_start(_as_utc(now))
    for _ in range(_SESSION_SEARCH_MAX_DAYS):
        if (is_market_open(boundary)
                and not is_market_open(boundary - timedelta(minutes=1))):
            return boundary
        boundary -= timedelta(days=1)
    raise ValueError(
        f"no closed-to-open transition within {_SESSION_SEARCH_MAX_DAYS} "
        f"days before {now.isoformat()}")
```

- [ ] `src/agentic_fx/core/scheduler.py` `_trade_mission_due`: 段 a で入れた `self._due_cron_watermarks = due` の直前に 2 行追加:

```python
        if due:
            due = self._drop_previous_session_bars(due, now)
```

  `_record_cron_coalesced` の後にメソッド追加:

```python
    def _drop_previous_session_bars(self, due: dict, now: datetime) -> dict:
        """`W + 足幅 <= session_start(now)` の足 (前セッションの足) では
        起動しない。cursor はその足まで進め、1 回だけ記録する。"""
        session = market_hours.session_start(now)
        width = self.settings.datafeed.decision_timeframe_width
        current = {key: bar for key, bar in due.items() if bar + width > session}
        previous = {key: bar for key, bar in due.items() if bar + width <= session}
        if previous:
            self._advance_cron_watermarks(previous)
            for (pair, interval), bar_time in sorted(previous.items()):
                self.activity.write(
                    Category.SYSTEM, "cron_previous_session_bar_skipped",
                    f"{pair} {interval} bar={_fmt_bar(bar_time)} "
                    f"session_start={_fmt_bar(session)}")
        return current
```

  (`market_hours.session_start` はモジュール属性として呼ぶ — テストの `monkeypatch.setattr(market_hours, "session_start", ...)` が効く形)

- [ ] **Critical (実測で判明、2026-09-26 着手前検証)**: 既存 `tests/core/test_scheduler.py:3091` (`test_closed_tick_baselines_cursor_only_when_state_is_ready`) は、degraded のまま週末をまたぎ、日曜 22:00 の開場 tick で金曜 10:59:30 足 (1h) の mission が 1 回起動することを assert している。IV-3 (前セッション足の skip) 適用後は金曜の足が前セッション足になり skip される (spec §3.2「resume 後の最初の開場 tick は、前セッションの足でなければ最新 1 本で発火する」) ため、この既存テストは無改変では red になる。設計書 §10・spec との食い違いは「既存 scheduler テストは assert 無改変で green」としていたが、この 1 本は AC-B2-21(b) の意図的な差分に当たるので書換え対象に追加する。書換え案 (degraded 中に cursor を進めない前半の assert は残す):

```python
    env.sched.tick(opened)
    assert env.trade_calls == 0
    assert env.sched._cron_watermarks[("USDJPY", "1h")] == (
        FRI - timedelta(hours=1, seconds=30))
    # activity に cron_previous_session_bar_skipped が 1 行
    # (USDJPY 1h bar=2026-07-24T10:59 session_start=2026-07-26T21:00)
    env.sched.tick(opened + timedelta(minutes=1))
    assert env.trade_calls == 0
```

  docstring も「前セッションの足なので起動しない」に直す。degraded→resume→同一セッションで発火する側を pin したいなら、別のテスト (`test_closed_tick_baselines_cursor_when_state_stays_ready` の対照側など) を新設して週中に resume させる形にする。

### Step 1-c: green

- [ ] Step 1-a の 2 ファイル + `tests/core/test_scheduler.py tests/core/test_scheduler_signal.py tests/core/test_scheduler_tick_order.py` が pass。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): 転写直後は `7 failed, 151 passed`** (`session_start` の 6 例が import 漏れで `NameError` のまま + 上記 `test_closed_tick_baselines_cursor_only_when_state_is_ready` の `assert 0 == 1`)。**import 追記と上記書換えの後は `158 passed`。** 既存 `test_cron_none_pair_recovers_and_closed_restart_only_baselines` (金曜 22:00 起点) は assert 無改変で green。

### Step 1-d: 逆変異

| # | 対象 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| Tb1-M1 | `market_hours.py` `session_start` | 遡り探索の本体 | `return (boundary - timedelta(days=(boundary.weekday() + 1) % 7)).replace(hour=21, minute=0, second=0, microsecond=0)` (直近の日曜 21:00 固定) | `test_session_start_...` の 2025-12-26 例 / `test_holiday_not_joined_to_weekend_skips_pre_holiday_bar` |
| Tb1-M2 | 同 | `and not is_market_open(boundary - timedelta(minutes=1))` | (条件を削除: 開場中の境界なら何でも返す) | `test_session_start_...` の週中例 / AC-B2-05 |
| Tb1-M3 | 同 | 遡り探索の本体 | 直近の境界から遡って「金曜 21:00 か日曜 21:00 の新しい方」を返す (曜日パターン) | `test_session_start_...` の 2025-12-26 例 (祝日明けの木曜 21:00) と `test_holiday_not_joined_to_weekend_skips_pre_holiday_bar`。**実測で判明 (2026-09-26 着手前検証): 2026-12-28・2027-01-04 の例はこの変異を殺さない** — scheduler は開場中にしか `session_start` を呼ばず (閉場中の tick は早期 return で受理ブロックに届かない)、開場中なら「直近の金/日 21:00 の新しい方」は常に正しい日曜 21:00 と一致するため。**この構造 (scheduler 経由では AC-B2-08 が挙げる失敗モード ―「金 21:00 を開場境界と誤る」― は起きない) は spec には未反映**。単体の `session_start` 例 (2025-12-26) と `test_holiday_not_joined_to_weekend_skips_pre_holiday_bar` (AC-B2-06 相当) だけが検出対象になる |
| Tb1-M4 | `scheduler.py` `_drop_previous_session_bars` | `if bar + width > session` | `if bar > session` (足の開始で比べる → 日曜 21:00 足まで skip) | AC-B2-05 (日 21:15:30 に起動しない) |
| Tb1-M5 | 同 | `if bar + width > session` | `if bar.date() >= session.date()` (UTC 日付で比べる) | AC-B2-05 / AC-B2-07 |
| Tb1-M6 | 同 | `self._advance_cron_watermarks(previous)` | (行削除: cursor を進めない) | 4 本の `_cron_watermarks[KEY] == ...20:45` |
| Tb1-M7 | `scheduler.py` `_trade_mission_due` | `due = self._drop_previous_session_bars(due, now)` | (2 行削除) | AC-B2-05〜08・21 (b) |
| Tb1-M8 | `scheduler.py` `tick` | 受理ブロックの位置 | 受理ブロックを `try:` 先頭へ移す | `test_session_start_exception_leaves_day_close_and_sl_already_done` |

**実測 (2026-09-26、worktree `tmp/wt/b2-verify`)**: M1 殺す (`session_start[now3]` (2025-12-26) と `test_holiday_not_joined_to_weekend_skips_pre_holiday_bar` が red)。M3 殺す (同じ 2 本のみ、2026-12-28・2027-01-04 の例は殺さない、上記参照)。M4 殺す (5 本 red)。M2・M5〜M8 は表のとおり実施予定 (未個別実測)。

- [ ] commit: `feat(decision-timeframe): 閉場明けの最初の tick で前セッションの確定足に判断 mission を起こさない (session_start を閉場→開場の状態遷移で定義)`

---

## Tb2: `cron_cursor` の永続化・復元・dirty 再試行・未来値の hold と自動復帰

### Step 2-a: テストを置いて red を確認する

- [ ] `tests/store/test_cron_cursor.py` を新規作成:

```python
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import cron_cursor, mission_decision_bars, missions, orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 9, 24, 13, 0, 41, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 24, 12, 45, tzinfo=timezone.utc)
T0 = T1 - timedelta(minutes=15)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_upsert_only_moves_forward_and_load_all_round_trips(tmp_path):
    conn = _conn(tmp_path)
    cron_cursor.upsert(conn, "USDJPY", "15m", T1, now=NOW)
    cron_cursor.upsert(conn, "USDJPY", "15m", T0, now=NOW)     # 後退は書かない
    cron_cursor.upsert(conn, "EURUSD", "15m", T0, now=NOW)
    assert cron_cursor.load_all(conn) == {
        ("USDJPY", "15m"): T1, ("EURUSD", "15m"): T0}


def test_upsert_failure_leaves_no_open_transaction(tmp_path):
    conn = _conn(tmp_path)
    conn.executescript(
        "CREATE TRIGGER fail_cc BEFORE INSERT ON cron_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END;")
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        cron_cursor.upsert(conn, "USDJPY", "15m", T1, now=NOW)
    assert not conn.in_transaction
    assert cron_cursor.load_all(conn) == {}


def test_init_db_adds_b2_tables_to_existing_db(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("DROP TABLE mission_decision_bars")
    conn.execute("DROP TABLE cron_cursor")
    conn.commit()
    init_db(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cron_cursor", "mission_decision_bars"} <= names


def test_init_db_on_pre_b2_schema_keeps_existing_rows_and_only_adds_new_tables(
        tmp_path):
    """B-2 の新表が無い旧 schema の DB に既存の監査行 (missions・orders) を
    入れてから `init_db` を当てても、既存行の値・件数は変わらず、新表
    だけが増える。"""
    conn = _conn(tmp_path)
    conn.execute("DROP TABLE mission_decision_bars")
    conn.execute("DROP TABLE cron_cursor")
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    conn.commit()
    oid = orders.insert(
        conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=NOW, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)
    before_missions = conn.execute(
        "SELECT COUNT(*) AS c FROM missions").fetchone()["c"]
    before_orders = conn.execute(
        "SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    init_db(conn)  # 旧 schema の DB に当てる (新規作成ではない)

    after_missions = conn.execute(
        "SELECT COUNT(*) AS c FROM missions").fetchone()["c"]
    after_order = conn.execute(
        "SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    assert after_missions == before_missions == 1
    assert dict(after_order) == dict(before_orders)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cron_cursor", "mission_decision_bars"} <= names
    assert mission_decision_bars.for_mission(conn, mid) == {}


def test_mission_decision_bars_round_trip_and_cascade(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    bars = {("USDJPY", "15m"): T1, ("EURUSD", "15m"): T0}
    mission_decision_bars.insert_many(conn, mid, bars)
    assert conn.in_transaction          # insert_many は commit しない
    conn.commit()
    assert mission_decision_bars.for_mission(conn, mid) == bars
    conn.execute("DELETE FROM missions WHERE id=?", (mid,))
    conn.commit()
    assert mission_decision_bars.for_mission(conn, mid) == {}
```

- [ ] `TABLE_NAMES` pin 2 本: `tests/store/test_db.py` の `EXPECTED` (`:10-17`) と `tests/store/test_db_migrations.py` の `test_table_names_include_candidate_archives` の frozenset (`:31-41`) の末尾 `"datafeed_outage_state", "datafeed_outage_gap",` の後に `"cron_cursor", "mission_decision_bars",` を足す (他の要素・assert は不変)。
- [ ] `tests/core/test_scheduler.py` の `test_open_restart_runs_latest_watermark_once` (`:306-315`) を置換 (設計書 §8-3 の裁定で旧仕様「再起動のたびに最新 1 本を再判断」を書き換える対象そのもの):

```python
def test_open_restart_restores_cursor_and_does_not_rerun_latest_watermark(tmp_path):
    env = Env(tmp_path, seed_cron_bar=False)
    _seed_decision_bar(env, WED - timedelta(hours=1, seconds=30))
    env.sched.tick(WED)
    assert env.trade_reasons == ["cron"]
    restarted = Env(tmp_path, seed_cron_bar=False)
    restarted.sched.tick(WED)
    assert restarted.trade_reasons == []
    restarted.sched.tick(WED + timedelta(minutes=10))
    assert restarted.trade_reasons == []
    _seed_decision_bar(restarted, WED)
    restarted.sched.tick(WED + timedelta(hours=1, seconds=30))
    assert restarted.trade_reasons == ["cron"]
```

- [ ] `tests/core/test_scheduler_decision_timeframe.py` に追記 (import に `from agentic_fx.store import cron_cursor` と `from agentic_fx.store.db import connect, init_db` を足す):

```python
def _fail_cursor_writes(conn):
    conn.executescript(
        "CREATE TRIGGER fail_cc_ins BEFORE INSERT ON cron_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected cron_cursor failure'); END;"
        "CREATE TRIGGER fail_cc_upd BEFORE UPDATE ON cron_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected cron_cursor failure'); END;")


def _allow_cursor_writes(conn):
    conn.executescript("DROP TRIGGER fail_cc_ins; DROP TRIGGER fail_cc_upd;")


def _preload_cursor(tmp_path, rows):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    for (pair, interval), bar_time in rows.items():
        cron_cursor.upsert(conn, pair, interval, bar_time, now=THU)
    conn.close()


def test_restart_restores_cursor_and_does_not_rejudge_same_bar(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert slot.accepted_at == [_at(THU, 13, 0, 41)]
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 45)}
    restarted, slot2 = _env(tmp_path, _at(THU, 13, 8, 43))
    _tick(restarted, slot2, _at(THU, 13, 8, 44))
    assert slot2.accepted_at == []
    _seed_bar(restarted, _at(THU, 13, 0))
    _tick(restarted, slot2, _at(THU, 13, 15, 30))
    assert slot2.accepted_at == [_at(THU, 13, 15, 30)]


def test_cursor_write_failure_is_retried_in_closed_ticks_once_per_tick(tmp_path):
    env, slot = _env(tmp_path, FRI)
    _seed_bar(env, _at(FRI, 20, 15))
    _tick(env, slot, _at(FRI, 20, 30, 30))
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(FRI, 20, 30))
    _tick(env, slot, _at(FRI, 20, 45, 41))
    assert len(slot.accepted_at) == 2
    assert env.sched._cron_watermarks[KEY] == _at(FRI, 20, 30)
    assert env.sched._dirty_cron_cursor == {KEY}
    assert not env.conn.in_transaction
    assert len(_activity(env, "cron_cursor_write_failed")) == 1
    for count, minute in ((2, 1), (3, 2)):
        _tick(env, slot, _at(FRI, 21, minute, 30))   # 閉場 tick (早期 return)
        assert env.sched._was_open is False
        assert len(_activity(env, "cron_cursor_write_failed")) == count
        assert not env.conn.in_transaction
    assert cron_cursor.load_all(env.conn)[KEY] == _at(FRI, 20, 15)
    _allow_cursor_writes(env.conn)
    _tick(env, slot, _at(FRI, 21, 3, 30))
    assert env.sched._dirty_cron_cursor == set()
    assert cron_cursor.load_all(env.conn)[KEY] == _at(FRI, 20, 30)
    _tick(env, slot, _at(FRI, 21, 4, 30))
    assert len(_activity(env, "cron_cursor_write_failed")) == 3


def test_cursor_write_failure_is_retried_while_outage_is_degraded(tmp_path):
    state = {"value": "ready"}
    env = Env(tmp_path, base=THU, seed_cron_bar=False,
              state_fn=lambda: state["value"])
    env.sched.settings = _settings()
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert env.sched._dirty_cron_cursor == {KEY}
    state["value"] = "degraded"
    _allow_cursor_writes(env.conn)
    _tick(env, slot, _at(THU, 13, 1, 41))
    assert env.sched._dirty_cron_cursor == set()
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 45)}
    assert len(slot.accepted_at) == 1


def test_cursor_write_failure_is_logged_once_per_tick_even_with_retry_and_accept(
        tmp_path):
    """同じ tick で finally の再試行と新しい受理の upsert が両方失敗しても
    cron_cursor_write_failed は 1 行。"""
    env, slot = _env(tmp_path, THU)
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert len(_activity(env, "cron_cursor_write_failed")) == 1
    _seed_bar(env, _at(THU, 13, 0))
    _tick(env, slot, _at(THU, 13, 15, 41))          # 再試行 + 受理の 2 回失敗
    assert len(slot.accepted_at) == 2
    assert len(_activity(env, "cron_cursor_write_failed")) == 2
    assert env.sched._dirty_cron_cursor == {KEY}
    assert env.sched._cron_watermarks[KEY] == _at(THU, 13, 0)


def test_cursor_write_failure_logs_again_on_next_tick_even_with_same_now(
        tmp_path):
    """`now` の値が同一のまま tick が連続して呼ばれても (時計の解像度や
    テストのヘルパ都合で起き得る)、2 回目の tick 呼び出しの失敗はちゃんと
    記録される — `now` の同値性で「同じ tick」を判定すると無音になる。"""
    env, slot = _env(tmp_path, THU)
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    same_now = _at(THU, 13, 0, 41)
    _tick(env, slot, same_now)
    assert len(_activity(env, "cron_cursor_write_failed")) == 1
    _tick(env, slot, same_now)          # 同じ now で 2 回目の tick 呼び出し
    assert len(_activity(env, "cron_cursor_write_failed")) == 2


def test_unpersisted_cursor_is_rejudged_after_restart_at_least_once(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 41))
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert env.sched._dirty_cron_cursor == {KEY}
    restarted, slot2 = _env(tmp_path, _at(THU, 13, 8, 43))
    _allow_cursor_writes(restarted.conn)
    _tick(restarted, slot2, _at(THU, 13, 8, 44))
    assert slot2.accepted_at == [_at(THU, 13, 8, 44)]     # 12:45 を再判断
    assert cron_cursor.load_all(restarted.conn)[KEY] == _at(THU, 12, 45)


def test_future_cursor_holds_only_that_pair_and_resumes_when_w_catches_up(tmp_path):
    _preload_cursor(tmp_path, {("USDJPY", "15m"): _at(THU, 13, 15),
                               ("EURUSD", "15m"): _at(THU, 12, 45)})
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 13, 0), pair=pair)
    _tick(env, slot, _at(THU, 13, 15, 30))
    assert slot.accepted_at == [_at(THU, 13, 15, 30)]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 0)}
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 13, 15)
    assert _activity(env, "cron_cursor_future_watermark") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T13:00 "
        "(この pair の cron 判定を保留、W が L に追いつくと自動で再開)"]
    _tick(env, slot, _at(THU, 13, 16, 30))
    assert len(_activity(env, "cron_cursor_future_watermark")) == 1
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 13, 15), pair=pair)
    _tick(env, slot, _at(THU, 13, 30, 30))
    assert _activity(env, "cron_cursor_future_watermark_resolved") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T13:15 (cron 判定を再開)"]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 15)}
    _seed_bar(env, _at(THU, 13, 30), pair="USDJPY")
    _tick(env, slot, _at(THU, 13, 45, 30))
    assert env.sched._due_cron_watermarks == {("USDJPY", "15m"): _at(THU, 13, 30)}
    assert len(slot.accepted_at) == 3
    assert len(_activity(env, "cron_cursor_future_watermark_resolved")) == 1


def test_future_watermark_written_by_separate_connection_while_running_is_detected(
        tmp_path):
    """起動時の復元直後だけでなく、稼働中に別接続 (手動編集を模す) が
    `cron_cursor` へ未来値を書き込んだ場合も、次の tick で検出して hold
    に入る (`_refresh_cron_cursor_from_store` が毎 tick 永続値を読み直す
    ことの検証。メモリの `_cron_watermarks` だけを見ていると検出できない)。"""
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 41))
    assert slot.accepted_at == [_at(THU, 12, 45, 41)]
    # 手動編集・別プロセスからの書込みを模す (スケジューラのメモリ
    # `_cron_watermarks` を経由せず、DB に直接 upsert する)。
    cron_cursor.upsert(env.conn, "USDJPY", "15m", _at(THU, 13, 15), now=THU)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert slot.accepted_at == [_at(THU, 12, 45, 41)]      # 追加受理なし (hold)
    assert _activity(env, "cron_cursor_future_watermark") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T12:45 "
        "(この pair の cron 判定を保留、W が L に追いつくと自動で再開)"]


def test_restored_cursor_without_observed_w_is_left_unverified(tmp_path):
    _preload_cursor(tmp_path, {("USDJPY", "15m"): _at(THU, 12, 45),
                               ("EURUSD", "15m"): _at(THU, 12, 45)})
    state = {"value": "ready"}
    env = Env(tmp_path, base=THU, seed_cron_bar=False,
              state_fn=lambda: state["value"])
    env.sched.settings = _settings(pairs=("USDJPY", "EURUSD"))
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _seed_bar(env, _at(THU, 13, 0), pair="EURUSD")        # USDJPY の W は未観測
    _tick(env, slot, _at(THU, 13, 15, 30))
    assert slot.accepted_at == [_at(THU, 13, 15, 30)]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 0)}
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)
    assert _activity(env, "cron_cursor_future_watermark") == []
    state["value"] = "degraded"
    _seed_bar(env, _at(THU, 13, 0), pair="USDJPY")
    _tick(env, slot, _at(THU, 13, 16, 30))
    assert len(slot.accepted_at) == 1
    state["value"] = "ready"
    _tick(env, slot, _at(THU, 13, 17, 30))
    assert slot.accepted_at[-1] == _at(THU, 13, 17, 30)
    assert env.sched._due_cron_watermarks == {("USDJPY", "15m"): _at(THU, 13, 0)}
```

- [ ] red を確認する: `uv run pytest -q tests/store/test_cron_cursor.py tests/store/test_db.py tests/store/test_db_migrations.py tests/core/test_scheduler.py::test_open_restart_restores_cursor_and_does_not_rerun_latest_watermark tests/core/test_scheduler_decision_timeframe.py`
      期待: store は `ImportError: cannot import name 'cron_cursor' from 'agentic_fx.store'` (collection error)、`tests/core/test_scheduler_decision_timeframe.py` も同じ import で collection error。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): store 側は期待どおり collection error。** store 部分を先に green にした後の scheduler 側の個別 red: `test_open_restart_restores_cursor_and_does_not_rerun_latest_watermark` は `assert ['cron'] == []`、restart 版は `assert {} == {('USDJPY','15m'): 12:45}` 系、dirty 系 3 本は `AttributeError: 'Scheduler' object has no attribute '_dirty_cron_cursor'`、logged-once は `assert 0 == 1`、future watermark 系は due 不一致、restored 系は `KeyError: ('USDJPY', '15m')` — いずれも想定内の理由。`TABLE_NAMES` pin 2 本は DDL と同時に追加するため単独の red は取らない (store 実装後に green を確認する)。

### Step 2-b: 実装を転写する

- [ ] `src/agentic_fx/store/db.py`: `_SCHEMA` の `datafeed_outage_gap` の DDL の閉じ括弧 `);` の直後 (`""" + _IMPROVE_WAVES_DDL` の前) に追記し、`TABLE_NAMES` の末尾に `"cron_cursor", "mission_decision_bars",` を足す:

```sql
CREATE TABLE IF NOT EXISTS cron_cursor (
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,
  bar_time TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (pair, interval)
);
CREATE TABLE IF NOT EXISTS mission_decision_bars (
  mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,
  bar_time TEXT NOT NULL,
  PRIMARY KEY (mission_id, pair, interval)
);
```

  既存 DB は `init_db` 冒頭の `executescript(_SCHEMA)` (IF NOT EXISTS) で新表が足されるだけで、既存行は変更しない (`candidate_archives` と同じ経路)。
- [ ] `src/agentic_fx/store/cron_cursor.py` を新規作成:

```python
"""判断足 cron の処理済み watermark (L) の永続化。

行は (pair, interval) 単位。bar_time は既存値と新値の max でのみ前進する。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("cron_cursor: naive datetime は受け付けない")
    return value.astimezone(timezone.utc).isoformat()


def upsert(conn: sqlite3.Connection, pair: str, interval: str,
           bar_time: datetime, *, now: datetime) -> None:
    # with conn: 例外時は ROLLBACK し、呼び出し側の接続に未確定の
    # transaction を残さない。
    with conn:
        conn.execute(
            "INSERT INTO cron_cursor (pair, interval, bar_time, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(pair, interval) DO UPDATE SET "
            "bar_time=MAX(cron_cursor.bar_time, excluded.bar_time), "
            "updated_at=excluded.updated_at",
            (pair, interval, _iso(bar_time), _iso(now)))


def load_all(conn: sqlite3.Connection) -> dict[tuple[str, str], datetime]:
    rows = conn.execute(
        "SELECT pair, interval, bar_time FROM cron_cursor").fetchall()
    return {(r["pair"], r["interval"]):
            datetime.fromisoformat(r["bar_time"]).astimezone(timezone.utc)
            for r in rows}
```

- [ ] `src/agentic_fx/store/mission_decision_bars.py` を新規作成:

```python
"""cron mission が前進させた (pair, 判断足) ごとの確定足 (provenance)。"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone


def insert_many(conn: sqlite3.Connection, mission_id: int,
                bars: Mapping[tuple[str, str], datetime]) -> None:
    """commit しない。missions.start(commit=False) と同じ transaction で
    呼び出し側が commit / rollback する。"""
    conn.executemany(
        "INSERT INTO mission_decision_bars (mission_id, pair, interval, "
        "bar_time) VALUES (?,?,?,?)",
        [(mission_id, pair, interval,
          bar_time.astimezone(timezone.utc).isoformat())
         for (pair, interval), bar_time in sorted(bars.items())])


def for_mission(conn: sqlite3.Connection,
                mission_id: int) -> dict[tuple[str, str], datetime]:
    rows = conn.execute(
        "SELECT pair, interval, bar_time FROM mission_decision_bars "
        "WHERE mission_id=?", (mission_id,)).fetchall()
    return {(r["pair"], r["interval"]):
            datetime.fromisoformat(r["bar_time"]).astimezone(timezone.utc)
            for r in rows}
```

- [ ] `src/agentic_fx/core/scheduler.py`:
  - import: `from agentic_fx.store import cron_cursor, ohlcv, orders`
  - `__init__`: `self._warned_regressed_cron_watermarks: set[...] = set()` を `self._cron_hold: set[tuple[str, str]] = set()` に改名し (参照は `_trade_mission_due` の 1 か所だけ、下で置換)、`__init__` の末尾 (`self._closed_bar_unavailable_pairs` の後) に:

```python
        # 永続化に失敗した (pair, interval)。同一プロセスが生きている間は
        # tick の finally で再試行する (at-least-once)。
        self._dirty_cron_cursor: set[tuple[str, str]] = set()
        # tick ごとにリセットするローカルフラグ。`now` の同値性で判定
        # すると、同じ `now` で tick が 2 回呼ばれる再入 (テストのヘルパが
        # 同一 `now` を渡し直す場合など) で 2 回目の失敗が無音になるため、
        # tick 呼び出し回数そのものを数えるフラグにする。
        self._cron_cursor_failure_logged_this_tick = False
        self._restore_cron_cursor()
```

  - `tick` 本体の**冒頭** (引数を受け取った直後、`try:` より前) に 1 行追加: `self._cron_cursor_failure_logged_this_tick = False` (I2 是正: `now` の値で判定していた同一 tick 検出を、tick 呼び出し 1 回ごとに必ずリセットするローカルフラグに変える — 同じ `now` で tick が連続して呼ばれても 2 回目の失敗はちゃんと記録される)。
  - `tick` の `finally:` の `self._run_hooks(now)` の直後に 1 行: `self._retry_dirty_cron_cursor(now)`
  - 受理ブロックの受理側を置換:

```python
                if result.accepted:
                    previous = dict(self._cron_watermarks)
                    advanced = self._advance_cron_watermarks(
                        self._pending_cron_watermarks)
                    self._cron_deferred_logged.clear()
                    self._persist_cron_cursor(advanced, now)
                    self._record_cron_coalesced(previous)
```

  - **spec §3.2 の稼働中検出への是正: `_trade_mission_due` の `latest = self._latest_cron_watermarks(now)` の直後に 1 行追加**: `self._refresh_cron_cursor_from_store()`。**理由**: 稼働中に別接続・手動編集で `cron_cursor` の永続値が書き換わっても、メモリの `_cron_watermarks` だけを見ていては検出できない (起動時の復元直後にしか未来値 hold が働かない、というプラン草稿の食い違い #11 は spec 側が正しいという裁定)。毎 tick、永続 L を読み直して既存メモリ値との max でメモリへ反映してから、以下の pair ループで L/W を比較する。upsert が 0.02〜0.03ms/tick (§9-5 実測) なので読み込みも同程度に安価。
  - `_drop_previous_session_bars` (Tb1) の後、`_hold_future_cursor`/`_release_future_cursor` の前にメソッド追加:

```python
    def _refresh_cron_cursor_from_store(self) -> None:
        """毎 tick、永続 `cron_cursor` を読み直し、既存メモリ値との max で
        反映する。稼働中に別接続や手動編集で永続値が変わっても (起動時の
        復元直後だけでなく) 同じ tick で検出できるようにする。読込み自体の
        失敗はメモリの現状維持で握りつぶす — 検出の劣化はあっても tick を
        止めない。"""
        try:
            persisted = cron_cursor.load_all(self.conn)
        except Exception:  # noqa: BLE001 — 読込み失敗でも tick は続行する
            _log.exception("cron cursor refresh failed")
            return
        for key, bar_time in persisted.items():
            mem = self._cron_watermarks.get(key)
            if mem is None or bar_time > mem:
                self._cron_watermarks[key] = bar_time
```

  - `_trade_mission_due` の pair ループを置換:

```python
        for key, watermark in latest.items():
            cursor = self._cron_watermarks.get(key)
            if cursor is not None and watermark < cursor:
                self._hold_future_cursor(key, cursor, watermark)
                continue
            if key in self._cron_hold:
                self._release_future_cursor(key, cursor, watermark)
            if cursor is None or watermark > cursor:
                due[key] = watermark
```

  - `_baseline_cron_watermarks` と `_advance_cron_watermarks` を置換:

```python
    def _baseline_cron_watermarks(self, now: datetime) -> None:
        advanced = self._advance_cron_watermarks(self._latest_cron_watermarks(now))
        # 前進した pair だけ書く (閉場中に同じ値を毎 tick 書き直さない)
        self._persist_cron_cursor(advanced, now)

    def _advance_cron_watermarks(self, latest: dict) -> list[tuple[str, str]]:
        # cursor は key ごとに単調にしか進めない。別 pair の新しい足で受理した tick に
        # 後退中の pair が混ざっても、その pair の cursor を巻き戻さない (巻き戻すと
        # 元の足が戻ったときに同じ足で再発火する)
        advanced: list[tuple[str, str]] = []
        for key, bar_time in latest.items():
            cur = self._cron_watermarks.get(key)
            if cur is None or bar_time > cur:
                self._cron_watermarks[key] = bar_time
                advanced.append(key)
        return advanced
```

  - `_drop_previous_session_bars` (Tb1) の `self._advance_cron_watermarks(previous)` を `self._persist_cron_cursor(self._advance_cron_watermarks(previous), now)` に。
  - `_drop_previous_session_bars` の後にメソッド追加:

```python
    def _restore_cron_cursor(self) -> None:
        try:
            restored = cron_cursor.load_all(self.conn)
        except Exception as e:  # noqa: BLE001 — 空の cursor で起動する (現行どおり最新 1 本で発火)
            text = safe_error_text(e)
            _log.warning("cron cursor restore failed: %s", text)
            self.activity.write(
                Category.SYSTEM, "cron_cursor_restore_failed",
                f"{text} — 空の cursor で起動 (最新の確定足 1 本で発火し得る)")
            return
        self._cron_watermarks.update(restored)

    def _persist_cron_cursor(self, keys, now: datetime) -> None:
        for key in keys:
            bar_time = self._cron_watermarks.get(key)
            if bar_time is None:
                self._dirty_cron_cursor.discard(key)
                continue
            try:
                cron_cursor.upsert(self.conn, key[0], key[1], bar_time, now=now)
            except Exception as e:  # noqa: BLE001 — 永続化の失敗で tick を止めない (at-least-once)
                self._dirty_cron_cursor.add(key)
                self._note_cron_cursor_write_failure(key, bar_time, e, now)
            else:
                self._dirty_cron_cursor.discard(key)

    def _retry_dirty_cron_cursor(self, now: datetime) -> None:
        # tick の finally から呼ぶ: 閉場中・degraded 中の早期 return を含む
        # 全 return 経路で再試行する。
        if not self._dirty_cron_cursor:
            return
        try:
            self._persist_cron_cursor(sorted(self._dirty_cron_cursor), now)
        except Exception:  # noqa: BLE001 — finally から例外を漏らさない
            _log.exception("cron cursor retry failed")

    def _note_cron_cursor_write_failure(self, key, bar_time: datetime,
                                        error: Exception, now: datetime) -> None:
        text = safe_error_text(error)
        _log.warning("cron cursor write failed for %s %s: %s", key[0], key[1], text)
        # 失敗が続く間は tick ごとに 1 回だけ書く (同じ tick の受理と再試行で
        # 二重に書かない)。tick 呼び出し 1 回ごとにリセットするローカル
        # フラグで判定する — `now` の値で判定すると同一 `now` の再入
        # (テストの `_tick` ヘルパが同じ時刻で 2 回呼ぶ場合など) で無音に
        # なる。
        if self._cron_cursor_failure_logged_this_tick:
            return
        self._cron_cursor_failure_logged_this_tick = True
        self.activity.write(
            Category.SYSTEM, "cron_cursor_write_failed",
            f"{key[0]} {key[1]} bar={_fmt_bar(bar_time)} "
            f"dirty={len(self._dirty_cron_cursor)}: {text} — 次 tick で再試行 "
            "(成功までの間に再起動すると同じ足を再判断し得る)")

    def _hold_future_cursor(self, key, cursor: datetime,
                            watermark: datetime) -> None:
        if key in self._cron_hold:
            return
        self._cron_hold.add(key)
        _log.warning("cron watermark regressed for %s %s: %s < %s",
                     key[0], key[1], watermark.isoformat(), cursor.isoformat())
        self.activity.write(
            Category.SYSTEM, "cron_cursor_future_watermark",
            f"{key[0]} {key[1]} L={_fmt_bar(cursor)} W={_fmt_bar(watermark)} "
            "(この pair の cron 判定を保留、W が L に追いつくと自動で再開)")

    def _release_future_cursor(self, key, cursor: datetime,
                               watermark: datetime) -> None:
        self._cron_hold.discard(key)
        self.activity.write(
            Category.SYSTEM, "cron_cursor_future_watermark_resolved",
            f"{key[0]} {key[1]} L={_fmt_bar(cursor)} W={_fmt_bar(watermark)} "
            "(cron 判定を再開)")
```

  既存 `test_cron_watermark_regression_does_not_retrigger_or_rewind_cursor` の `caplog.text.count("cron watermark regressed") == 1` は `_hold_future_cursor` が同じ文言を 1 回だけ出すので assert 無改変で green のこと。**IV-10 の「稼働中に cron_cursor を読み直した際」(I3 是正、上記 `_refresh_cron_cursor_from_store` 参照): 本実装は稼働中も毎 tick 永続値を読み直し、メモリの L との max で反映してから比較する** — 起動時の復元直後だけでなく、別接続・手動編集で永続値が変わった場合も同じ tick で検出できる (プラン草稿時点の食い違い #11 は「稼働中は読み直さない」としていたが、これは spec の記述を落としていた差異であり、記述の具体化ではなく実装上の欠落だったため撤回する。2026-09-26 裁定)。

- [ ] **`tests/test_service_app.py` の既存 3 本 (`test_on_trade_mission_runs_loop_and_reflection` `:436`・`test_on_trade_mission_wrapper_also_runs_reflection` `:478`・`test_tick_propagates_trigger_to_missions_row` `:610` 付近) の `app.scheduler.tick(...)` 呼び出しを `with app.core_lock:` で包む** (assert は不変)。「flake が出たら直す」という事後対応にはしない — Tb2 の受理直後に `cron_cursor.upsert` (`with conn_core:`) が入り、supervisor スレッドの prepare と同じ `conn_core` を core_lock なしで同時に触り得るようになるため、本 Step で先回りして直す。

### Step 2-c: green

- [ ] Step 2-a の対象 + `tests/core/test_scheduler.py tests/core/test_scheduler_signal.py tests/core/test_scheduler_tick_order.py tests/core/test_scheduler_cache_maintenance.py tests/store` が pass。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): `640 passed`** (指定ファイル一式。この実測は本改訂で追加した 3 本 (`test_cursor_write_failure_logs_again_on_next_tick_even_with_same_now`・`test_future_watermark_written_by_separate_connection_while_running_is_detected`・`test_init_db_on_pre_b2_schema_keeps_existing_rows_and_only_adds_new_tables`) を含まない — 再実行時の期待値は `643 passed`)。既存 `test_cron_watermark_regression_does_not_retrigger_or_rewind_cursor` も無改変で green。

### Step 2-d: 逆変異

| # | 対象 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| Tb2-M1 | `scheduler.py` `__init__` | `self._restore_cron_cursor()` | (行削除) | `test_restart_restores_cursor_and_does_not_rejudge_same_bar` / `test_open_restart_restores_cursor_...` / AC-B2-11 |
| Tb2-M2 | `scheduler.py` `tick` | `finally` の `self._retry_dirty_cron_cursor(now)` | (行削除し、受理ブロックの `if result.accepted:` の中へ移す) | `test_cursor_write_failure_is_retried_in_closed_ticks_once_per_tick` / `test_cursor_write_failure_is_retried_while_outage_is_degraded` |
| Tb2-M3 | `scheduler.py` `_note_cron_cursor_write_failure` | `if self._cron_cursor_failure_logged_this_tick:` + `return` | (2 行削除) | `test_cursor_write_failure_is_logged_once_per_tick_even_with_retry_and_accept` (同じ tick の再試行と受理で 2 行になる) |
| Tb2-M4 | 同 | `self._cron_cursor_failure_logged_this_tick = True` | (行削除) | 同上 |
| Tb2-M5 | `scheduler.py` `tick` 冒頭 | `self._cron_cursor_failure_logged_this_tick = False` | (行削除、tick 呼び出しをまたいでリセットされなくなる) | `test_cursor_write_failure_logs_again_on_next_tick_even_with_same_now` / `test_cursor_write_failure_is_retried_in_closed_ticks_once_per_tick` (2 tick 目以降も記録されなくなる) |
| Tb2-M14 | `scheduler.py` `_trade_mission_due` | `self._refresh_cron_cursor_from_store()` | (行削除、起動時復元だけに戻す) | `test_future_watermark_written_by_separate_connection_while_running_is_detected` |
| Tb2-M6 | `cron_cursor.py` `upsert` | `with conn:` + `conn.execute(...)` | `conn.execute(...)` + `conn.commit()` | `test_upsert_failure_leaves_no_open_transaction` / closed-ticks テストの `not env.conn.in_transaction` |
| Tb2-M7 | 同 | `bar_time=MAX(cron_cursor.bar_time, excluded.bar_time)` | `bar_time=excluded.bar_time` | `test_upsert_only_moves_forward_and_load_all_round_trips` |
| Tb2-M8 | `scheduler.py` `_trade_mission_due` | `self._hold_future_cursor(...)` + `continue` | 未来値の pair を黙って採用 (`self._cron_watermarks[key] = watermark` して `continue`) | `test_future_cursor_holds_only_that_pair_...` (cursor 13:15 が 13:00 に巻き戻る) |
| Tb2-M9 | 同 | `if key in self._cron_hold: self._release_future_cursor(...)` | (2 行削除) | 同上 (resolved が出ない) |
| Tb2-M10 | 同 `_release_future_cursor` | `self._cron_hold.discard(key)` | (行削除) | 同上 (resolved が毎 tick 出る) |
| Tb2-M11 | 同 `_hold_future_cursor` | `continue` (ループ内) | `return None` (全 pair の判定を止める) | 同上 (EURUSD が起動しない) / AC-B2-11 |
| Tb2-M12 | `scheduler.py` `_baseline_cron_watermarks` | `self._persist_cron_cursor(advanced, now)` | `self._persist_cron_cursor(list(self._cron_watermarks), now)` (前進していない pair も毎 tick 書く) | 検出テスト無し — 生存が見込まれる (機能は同値で書込み量だけ増える)。**実測で判明 (2026-09-26、`tests/core tests/store` 1099 本で生存を確認、予告どおり)**: 生存 (等価、性能のみ) として記録し、書込み回数の spy テストは追加しない (機能差が無いので観測点を新設しない判断) |
| Tb2-M13 | `store/db.py` | `cron_cursor` の DDL | (削除) | store 4 本 / pin 2 本 |

Tb2-M12 のように機能が同値で観測点を持たない変異は、実走で生存したら報告に列挙する (リストは下限、[[mutation-testing]])。

**実測 (2026-09-26、worktree `tmp/wt/b2-verify`)**: M1 殺す (4 本 red)。M2 殺す (closed-ticks と degraded の 2 本 red)。M3 殺す (logged-once が red)。M4〜M11・M13・M14 は表のとおり実施予定 (未個別実測、M12 のみ上記のとおり実測済み)。

- [ ] commit: `feat(decision-timeframe): cron の処理済み watermark を cron_cursor に永続化して再起動で同じ足を再判断しない (書込み失敗は dirty 再試行、未来値は pair 単位で保留し自動復帰)`

---

## Tb3: watermark snapshot の受渡しと `missions` + `mission_decision_bars` の単一 transaction

### Step 3-a: テストを置いて red を確認する

- [ ] `tests/core/test_scheduler_decision_timeframe.py` に追記:

```python
def test_cron_passes_only_advanced_pairs_as_immutable_snapshot(tmp_path):
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    _seed_bar(env, _at(THU, 12, 30), pair="USDJPY")
    _seed_bar(env, _at(THU, 12, 45), pair="EURUSD")
    _tick(env, slot, _at(THU, 13, 0, 41))
    first = slot.accepted_kwargs[0]["decision_bars"]
    assert dict(first) == {("USDJPY", "15m"): _at(THU, 12, 30),
                           ("EURUSD", "15m"): _at(THU, 12, 45)}
    with pytest.raises(TypeError):
        first[("USDJPY", "15m")] = _at(THU, 13, 0)
    _seed_bar(env, _at(THU, 12, 45), pair="USDJPY")        # EURUSD は進まない
    _tick(env, slot, _at(THU, 13, 1, 41))
    assert dict(slot.accepted_kwargs[1]["decision_bars"]) == {
        ("USDJPY", "15m"): _at(THU, 12, 45)}
    assert dict(first) == {("USDJPY", "15m"): _at(THU, 12, 30),
                           ("EURUSD", "15m"): _at(THU, 12, 45)}


def test_signal_mission_gets_empty_decision_bars(tmp_path):
    env = Env(tmp_path, base=THU, seed_cron_bar=False,
              signal_due_fn=lambda now: True)
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _tick(env, slot, _at(THU, 12, 1))
    assert len(slot.accepted_kwargs) == 1
    assert dict(slot.accepted_kwargs[0]["decision_bars"]) == {}
```

- [ ] `tests/core/test_supervisor.py` に追記 (import に `from types import MappingProxyType`):

```python
def test_trade_job_forwards_decision_bars_only_when_given():
    seen = []

    def trade_fn(trigger, **kwargs):
        seen.append((trigger, dict(kwargs)))
        return trigger

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q)
    sup.start()
    bars = MappingProxyType({("USDJPY", "15m"): 1})
    sup.try_submit("trade", trigger="cron", decision_bars=bars).future.result(timeout=5.0)
    sup.try_submit("trade", trigger="signal").future.result(timeout=5.0)
    assert seen == [("cron", {"decision_bars": bars}), ("signal", {})]
    sup.shutdown(drain_exc=RuntimeError("test"))
```

- [ ] `tests/loops/test_trade_loop.py` に追記 (import に `from datetime import timedelta`、`from types import MappingProxyType`、`from agentic_fx.store import mission_decision_bars`):

```python
_OPEN_USDJPY = {"action": "open", "pair": "USDJPY", "direction": "long",
                "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
                "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
                "reasoning": "test"}


def test_cron_mission_records_decision_bars_per_pair_and_order_joins_back(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", _OPEN_USDJPY, [])])
    bars = {("USDJPY", "15m"): NOW - timedelta(minutes=45),
            ("EURUSD", "15m"): NOW - timedelta(minutes=30)}
    out = loop.run_once("cron", decision_bars=MappingProxyType(bars))
    assert out["result"] == "pending"
    mid = conn.execute("SELECT id FROM missions").fetchone()["id"]
    assert mission_decision_bars.for_mission(conn, mid) == bars
    row = conn.execute(
        "SELECT b.bar_time FROM orders o "
        "JOIN trade_intents i ON o.intent_id = i.id "
        "JOIN mission_decision_bars b ON b.mission_id = i.mission_id "
        "AND b.pair = o.pair WHERE o.pair = 'USDJPY'").fetchone()
    assert row["bar_time"] == "2026-07-22T11:15:00+00:00"


def test_decision_bars_insert_failure_rolls_back_mission_and_leaves_mid_none(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, [])])
    conn.executescript(
        "CREATE TRIGGER fail_mdb BEFORE INSERT ON mission_decision_bars "
        "BEGIN SELECT RAISE(ABORT, 'injected decision bars failure'); END;")
    out = loop.run_once("cron", decision_bars=MappingProxyType(
        {("USDJPY", "15m"): NOW - timedelta(minutes=15)}))
    assert out is None
    assert conn.execute("SELECT COUNT(*) c FROM missions").fetchone()["c"] == 0
    assert not conn.in_transaction
    assert runner.missions == []
    log = (tp / "a.log").read_text(encoding="utf-8")
    assert "mission_boundary_failed" in log
    assert "mission_finalize_conflict" not in log
```

- [ ] `tests/test_service_app.py` に追記 (`test_service_tick_records_deferred_with_supervisor_phase` の直後):

```python
def test_cron_tick_records_decision_bar_provenance_end_to_end(tmp_path):
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.core.supervisor import MissionSupervisor
    from agentic_fx.store import mission_decision_bars

    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"}, [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    bar_time = NOW - timedelta(hours=1, seconds=30)
    _seed_decision_bar(app.conn_core, bar_time)
    app.supervisor.start()
    try:
        captured = []
        original_try_submit = MissionSupervisor.try_submit

        def spy_try_submit(self, kind, **kw):
            r = original_try_submit(self, kind, **kw)
            if r.accepted:
                captured.append(r.future)
            return r

        with _no_real_network(), \
             patch.object(MissionSupervisor, "try_submit", spy_try_submit), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"), \
             patch.object(app.trade_loop.provider, "healthcheck",
                          return_value="yfinance"):
            # 本番と同じく core_lock の中で tick する (受理後の cron_cursor
            # upsert と supervisor スレッドの prepare が conn_core を同時に
            # 触らないように)
            with app.core_lock:
                app.scheduler.tick(NOW)
            assert len(captured) == 1
            captured[0].result(timeout=5.0)
        mid = app.conn_core.execute(
            "SELECT id FROM missions WHERE loop='trade'").fetchone()["id"]
        assert mission_decision_bars.for_mission(app.conn_core, mid) == {
            ("USDJPY", "1h"): bar_time}
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))
        app.close()
```

- [ ] red を確認する: `uv run pytest -q tests/core/test_scheduler_decision_timeframe.py tests/core/test_supervisor.py::test_trade_job_forwards_decision_bars_only_when_given tests/loops/test_trade_loop.py tests/test_service_app.py::test_cron_tick_records_decision_bar_provenance_end_to_end`
      期待: snapshot 2 本は `KeyError: 'decision_bars'`、supervisor は `assert [('cron', {}), ('signal', {})] == [('cron', {'decision_bars': ...}), ...]`、trade_loop 2 本は `TypeError: TradeLoop.run_once() got an unexpected keyword argument 'decision_bars'`、end-to-end は `assert {} == {('USDJPY', '1h'): 10:59:30}` 系。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): すべて期待どおり。**

### Step 3-b: 実装を転写する

- [ ] `src/agentic_fx/core/scheduler.py`: import に `from types import MappingProxyType`。受理ブロックの呼び出し 1 行を置換:

```python
            # その tick で前進した pair だけの確定足を不変の snapshot にして渡す。
            # 次 tick で _due_cron_watermarks が上書きされても受理済み mission
            # が見る値は変わらない。signal 起動では空。
            snapshot = MappingProxyType(
                dict(self._due_cron_watermarks) if reason == "cron" else {})
            result = self.on_trade_mission(reason, decision_bars=snapshot)
```

- [ ] `src/agentic_fx/core/supervisor.py`: `trade_fn` の型を `Callable[..., object]` にし、`_dispatch` の trade 分岐を置換:

```python
        if kind == "trade":
            extra = ({"decision_bars": kwargs["decision_bars"]}
                     if "decision_bars" in kwargs else {})
            trade_result = self._trade_fn(kwargs["trigger"], **extra)
            reflection_count = self._reflection_fn()
            return {"trade": trade_result,
                   "reflection_count": reflection_count}
```

- [ ] `src/agentic_fx/service.py`: `_trade_fn` (`:1001-1006`) と `on_trade_mission` (段 a で置換済みの定義) を置換:

```python
        def _trade_fn(trigger: str, decision_bars=None):
            # プラン 8 (Task 15): TradeLoop 自身が prepare/commit-core で
            # core_lock を保持する五相構造になったため、ここでは lock を
            # 掴まない (二重取得は RLock で技術的には安全だが、run 相の
            # 間ずっと lock を保持したままになり Task 15 の目的を無効化する)。
            return trade_loop.run_once(trigger, decision_bars=decision_bars)
```

```python
        def on_trade_mission(trigger: str, *, decision_bars) -> SubmitResult:
            # trigger は scheduler._trade_mission_due() が返した起動理由。
            # decision_bars はその tick に前進した (pair, 判断足) の確定足。
            # 受理/拒否と拒否理由をそのまま scheduler へ返す。
            return supervisor.try_submit("trade", trigger=trigger,
                                         decision_bars=decision_bars)
```

- [ ] `src/agentic_fx/loops/trade_loop.py`: import に `from collections.abc import Mapping`、`from datetime import datetime`、`from agentic_fx.store import mission_decision_bars`。`run_once` と `_run_once_impl` の署名に `decision_bars: Mapping[tuple[str, str], datetime] | None = None` を足し、`run_once` 本体を `return self._run_once_impl(trigger, decision_bars)` に。prepare の末尾 (`trade_loop.py:210-214`) の `if mid is None:` ブロックを置換:

```python
                if mid is None:
                    if trigger == "cron" and decision_bars:
                        mid = self._start_cron_mission(now, decision_bars)
                    else:
                        mid = missions.start(self.conn, "trade",
                                             self.settings.runner.trade.backend,
                                             self.settings.runner.trade.model, now,
                                             trigger=trigger)
```

  `_run_once_impl` の後にメソッド追加:

```python
    def _start_cron_mission(self, now, decision_bars) -> int:
        """missions 行と mission_decision_bars 行を 1 transaction で確定する
        (core_lock 保持中に呼ぶ)。例外時は両方を rollback して再送出する —
        呼び出し元の mid には何も代入されないので、外側 finally の
        finalize (commit されていない mission の終端) は走らない。"""
        try:
            mid = missions.start(self.conn, "trade",
                                 self.settings.runner.trade.backend,
                                 self.settings.runner.trade.model, now,
                                 trigger="cron", commit=False)
            mission_decision_bars.insert_many(self.conn, mid, decision_bars)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return mid
```

  (段 c が cron mission の `signals.claim_oldest` をこの commit の**後**に足す。本段では claim を呼ばない)

### Step 3-c: green

- [ ] Step 3-a の対象 + `tests/loops tests/core tests/test_service_app.py tests/test_wiring.py tests/test_closed_bars_wiring.py` が pass。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`、`-x` 付き): `1426 passed, 4 deselected`。core_lock の外で tick する既存 service テスト 3 本 (下記「未実測の申告」参照) も flake なし (1 回実行)。**

### Step 3-d: 逆変異

| # | 対象 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| Tb3-M1 | `scheduler.py` 受理ブロック | `dict(self._due_cron_watermarks) if reason == "cron" else {}` | `dict(self._pending_cron_watermarks) if reason == "cron" else {}` (全 pair を渡す) | `test_cron_passes_only_advanced_pairs_as_immutable_snapshot` (2 回目に EURUSD が混ざる) |
| Tb3-M2 | 同 | `MappingProxyType(dict(...))` | `self._due_cron_watermarks` (同じ dict を共有) | 同上 (`first` が次 tick で書き換わる / `TypeError` が出ない) |
| Tb3-M3 | `supervisor.py` `_dispatch` | `**extra` | (削除) | `test_trade_job_forwards_decision_bars_only_when_given` / end-to-end |
| Tb3-M4 | `service.py` `_trade_fn` | `decision_bars=decision_bars` | (削除) | `test_cron_tick_records_decision_bar_provenance_end_to_end` |
| Tb3-M5 | `trade_loop.py` `_start_cron_mission` | `commit=False` | `commit=True` | `test_decision_bars_insert_failure_rolls_back_mission_and_leaves_mid_none` (missions に 1 行残る) |
| Tb3-M6 | 同 | `self.conn.rollback()` | (行削除) | 同上 (`conn.in_transaction` が真・missions 1 行) |
| Tb3-M7 | `trade_loop.py` prepare | `mid = self._start_cron_mission(now, decision_bars)` | メソッドを使わず prepare に直書きし `mid = missions.start(..., commit=False)` を**先に mid へ代入**してから INSERT、例外時は rollback だけして `mid` を戻さない | 同上 (`mission_finalize_conflict` が出る) |
| Tb3-M8 | 同 | `if trigger == "cron" and decision_bars:` | `if False:` (子表を書かない) | `test_cron_mission_records_decision_bars_per_pair_and_order_joins_back` / end-to-end |

**実測 (2026-09-26、worktree `tmp/wt/b2-verify`)**: M1 殺す (`test_cron_passes_only_advanced_pairs_as_immutable_snapshot` red)。M5 殺す (`test_decision_bars_insert_failure_rolls_back_mission_and_leaves_mid_none` red)。M6 殺す (同上 red)。M2〜M4・M7・M8 は表のとおり実施予定 (未個別実測)。`run_once`/`_run_once_impl` の署名の改行位置はプラン未指定のため実装時に任意整形してよい (内容は不変)。

- [ ] commit: `feat(decision-timeframe): cron mission が前進させた pair ごとの確定足を mission_decision_bars に missions 行と同一 transaction で記録`

---

## 段 b 全体の green 確認 (Tb1〜Tb3 統合後、main へ入れる前)

- [ ] 既存 scheduler 安全系の全実行: 段 a ファイルの同名 Step と同じコマンド (`tests/core/test_scheduler*.py`・`test_supervisor.py`・`test_risk_gate.py`・`test_accounting.py`・`tests/test_stop_sequence.py`・`tests/backtest/test_kill_switch_replay.py`) + `tests/core/test_market_hours.py tests/store`。**実測は Tb3 Step 3-c の一括実行 (`tests/loops tests/core tests/test_service_app.py tests/test_wiring.py tests/test_closed_bars_wiring.py` で `1426 passed, 4 deselected`) がこの対象を包含している (2026-09-26、worktree `tmp/wt/b2-verify`)。それとは別に安全系だけを切り出した単独実行は着手時に指揮者が改めて実測する。**
- [ ] フルスイート: `uv run pytest -q -p no:cacheprovider --deselect tests/test_shell_interrupt.py --deselect "tests/test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end"`
      - baseline (`43c6042`、完全コマンドで実測): `1 failed, 4481 passed, 17 deselected in 658.56s` (failed は既存の環境依存 `test_init_completes_offline_with_unreachable_bridge`、単体では pass。B-2 と無関係)
      - 段 a+b 統合後 (worktree、`-x` なしの一覧取得): `11 failed, 4536 passed, 5 deselected in 665.06s`。11 件はすべて B-2 と無関係 (`test_init_completes_offline_with_unreachable_bridge` 1 本は同じ環境依存の JSONDecodeError、`tests/test_shell_interrupt.py` の 9 本 + `test_interactive_mode_actually_stops_via_stop_event_end_to_end` 1 本はこのコマンドの `--deselect` 条件では拾いきれない既知の stdin/stop_event 系)。deselect 条件を揃えると baseline 4482 本・統合後 4535 本で、差 **+53 が新規テスト** (parametrize 展開分を含む) に一致する。**この +53 は本改訂 (v1.1) で追加した 5 本 (Ta1 2 本・Tb2 3 本、段 a ファイルの Step 1-c・本ファイルの Step 2-c 参照) を含まない実測 — 再実行時の期待差分は +58。**
- [ ] `sqlite3` で tmp の新規 DB に `init_db` を当て、`cron_cursor`・`mission_decision_bars` の DDL (`PRAGMA foreign_key_list(mission_decision_bars)` が `missions` を `CASCADE` で指す) を目視確認。実 DB には当てない
- [ ] コメントの工程ラベル grep が 0 件、`git status` に実 DB・`config/settings.yaml`・`plugins/` の差分が無い
- [ ] 段 b を main へ統合

## 未実測の申告

- **§9-1 の「SIGKILL 後に `proc.wait` が 2 回起きる」経路**: measurements.md #1 で本環境では再現できず未検証。Cw の式には安全マージンとして残す (設計書どおり)。
- **§9-2 / §9-3**: 実 DB に該当例なし (日曜 21:0x の cron mission 0 件、signal の再 claim 0 件)。前セッション足の誤発火は AC-B2-05 の fake 再現に委ねる。今週末 (2026-09-26〜27) の実機観測で「現行コードが日曜 21:00:30 に金曜 20:45 足で起動するか」を段 b の main 投入**前**に確認できれば裏付けになる (設計書 §8 の裁定 2 の「9/27 21:00 UTC の実機観測後」に対応)。
- **cursor 復元で挙動が変わる既存テスト**: 同じ `tmp_path` の DB で `Env` / `build_app` を 2 回作るテストは grep で `tests/core/test_scheduler.py:311` (本プランで書換え)・`tests/test_service_app.py:823` (`test_f1c_startup_reclaim_recovers_claimed_signal`、signal の回収だけを見る)・`:4031` (`test_outage_state_and_gate_survive_process_restart`、degraded 中は due を見ない) の 3 本。後 2 本は cron の再発火を assert していないので無改変で green の見込み (推測)。取りこぼしはフルスイートで確定する。
- **core_lock の外で tick する既存 service テスト**: `tests/test_service_app.py` の `test_on_trade_mission_runs_loop_and_reflection` (`:436`)・`test_on_trade_mission_wrapper_also_runs_reflection` (`:478`)・`test_tick_propagates_trigger_to_missions_row` (`:610` 付近) は supervisor を起動した状態で `app.scheduler.tick(NOW)` を core_lock なしで呼ぶ。Tb2 以降は受理直後の `cron_cursor.upsert` (`with conn_core:`) と supervisor スレッドの prepare (`missions.start(commit=False)` + INSERT) が同じ `conn_core` を同時に触り得る (本番の `_scheduler_tick_once` は core_lock 内なので起きない)。**裁定 (2026-09-26): 採用。「flake が出たら直す」という事後対応にはせず、Tb2 の Step で最初からこの 3 本の `app.scheduler.tick(...)` 呼び出しを `with app.core_lock:` で包む (assert は不変)** — Tb2 の Step 2-b の実装転写が終わった時点で、この 3 本の書換えも同じ Step の作業項目に含める。実測 (2026-09-26、worktree `tmp/wt/b2-verify`、`-x` 付き 1 回実行) では 3 本とも flake は出なかったが、複数回実行での再現性は未検証のため上記の是正を先に適用する。
- **`on_trade_mission` fake が「呼ばれない」分類** (段 a ファイルの全数表の backtest・e2e_paper_cycle・test_wiring): 実行時に呼ばれないという判断は静的読取り (backtest の in-memory DB の `ohlcv_cache` が空) による推測。段 a の全体 green で確定する。
- **Tb2-M12**: 機能が同値 (書込み回数だけ増える) で観測点が無く、生存が見込まれる (表に明記)。実走結果で生存したものは報告に列挙する。
- 各 Step の red / green の逐語、フルスイートの baseline と本数は 2026-09-26 の着手前検証 (`tmp/wt/b2-verify`、詳細は `tmp/design-b2/plan-verify.md`) で実測し、本ファイル・段 a ファイルの該当箇所に転記済み。段 a・段 b をそれぞれ単独で main へ統合した直後の実測値 (この worktree は段 a→b を続けて実施したため未取得) は着手時に指揮者が改めて埋める。

## spec との食い違い (段 b 分、段 a ファイルの #1〜#9 に続く)

| # | 設計書の記述 | 現物 / 本プランの扱い | 印 |
|---|---|---|---|
| 10 | §10: `service.py` に「cursor 復元の配線」 | 復元は `Scheduler.__init__` で `self.conn` (= `conn_core`、`init_db` 済み) から読むので service.py の変更は不要。起動時の単一スレッド初期化中なので core_lock の外でも他スレッドと競合しない | **裁定 (2026-09-26): 採用。spec v1.1 で修正済み (§2, §3.2, §10, §11)** |
| 11 | §3.2 遷移表⑤「稼働中に他 pair の受理・baseline で `cron_cursor` を読み直した際にも同じ条件で検出」 | ~~稼働中は表を読み直さず、メモリの L (表と同値、max でのみ前進) を毎 tick W と比べる。検出条件と挙動は同じ~~ | **裁定 (2026-09-26): 撤回、spec 側が正しい。** 稼働中に別接続・手動編集で `cron_cursor` の永続値が変わっても、メモリだけを見ていると検出できない (プラン草稿のこの行は spec の稼働中検出を落としていた、記述の具体化ではなく実装上の欠落)。**Tb2 に `_refresh_cron_cursor_from_store` (毎 tick、永続 L を読み直して max でメモリに反映) を追加し、別接続書込みの逆変異テストを追加した (上記 Tb2 参照)** |
| 12 | §11: Tb1 と Tb2 は並列 (`┐ ┴→ Tb3`) | どちらも `_trade_mission_due` を変えるので scheduler 部分は直列 (store 部分だけ並列) | 裁定: 記述の具体化のみ (現状維持) |
| 13 | §3.5: 例外ハンドラで `mid = None` を代入してから再送出 | `_start_cron_mission` が成功時だけ値を返すので、呼び出し元の `mid` には例外時に何も代入されない (構造的に `None` のまま)。効果は同じ | **裁定 (2026-09-26): 採用。spec v1.1 で「構造的に成立する」旨に訂正済み (§3.5)** |
| 14 | §3.2 cron due 規則「前セッション足だけが due のとき L := W」 | 前セッション足と今セッション足が同じ tick に混在した場合も、前セッション側だけ skip して今セッション側は起動する (pair 単位で分ける) | **裁定 (2026-09-26): 採用。spec v1.1 で pair 単位の判定として明記済み (§3.2)** |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-26 | v1.0 | 初版。設計書 v1.0 の段 a (Ta1〜Ta3: Cw/Cd 起動時検査・watchdog 上限統一・派生足拒否 / SubmitResult と見送り・合流 activity / service 配線と全呼び出し元の書換え・資金保護順序) と段 b (Tb1〜Tb3: session_start と前セッション足 skip / cron_cursor の永続・復元・dirty 再試行・未来値 hold / snapshot と単一 transaction) を 2 ファイルに起こす。§9 実測 (`tmp/design-b2/measurements.md`) の #1・#5 を取り込み (式・値の変更なし)。spec との食い違い 14 件を列挙 | 設計書 v1.0 承認 (2026-09-26) を受けたプラン起草 | (未コミット) |
| 2026-09-26 | v1.1 | **着手前検証 (2026-09-26、Critical 3・Important 2・Minor 8) を反映**: Ta1 に `cw.total` と `cw.deadline` を区別する parametrize (Ta1-M4 の生存を解消)・`1d` の `intervals` 補正・red コマンド分割・ImportError 表記修正 (ab.md Ta1)。Ta2 の `test_supervisor.py:172` を無改変に訂正・Ta2-M1 に「finally 末尾の解決ブロックを削除する」を明記・`fail_pending` の古いコメントを更新 (ab.md Ta2)。Ta1 に `test_service_app.py:1409-1441` (`test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling`) の書換え (新 pin 値 518.0) を追加、Ta3 の characterization test (資金保護順序の既存 pin) を明示 (ab.md Ta1/Ta3)。Tb1 に `market_hours` の import 追記・`test_closed_tick_baselines_cursor_only_when_state_is_ready` の書換え (AC-B2-21(b) の意図的差分) を追加、Tb1-M3 の実測結果 (2025-12-26 例のみが殺す) を記録 (stage-b.md Tb1)。Tb2 に `_refresh_cron_cursor_from_store` (毎 tick の永続値再読込み)・tick ローカルフラグでの dirty ログ・migration の旧 DB 保持試験を追加 (stage-b.md Tb2)。`tests/test_service_app.py` の core_lock 外 tick 3 本を Tb2 の Step で最初から `with app.core_lock:` に包む。各 Step の red/green・逆変異・フルスイートの実測値 (`4536 passed`、baseline 比 +53、既存不変条件との衝突 2 件を含む) を記入。spec との食い違い表 #10〜#14 を裁定済みに更新 (#11 は撤回・spec 側が正) | 実装プラン執筆・着手前検証・外部レビューで判明した現物との差 | — |

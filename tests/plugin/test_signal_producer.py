"""signal_producer.SignalProducer のテスト (プラン 7 Task 8)。

sandbox サブプロセスは一切起動しない — 全テストで `sandbox_run` に fake を
注入する (`_FakeSandbox`)。実 HTTP/git/乱数/実時刻取得も使わない。

`H` は 2026-07-22T12:00 UTC (epoch 錨に対し 1h/4h いずれの境界にも整列 —
`tests/backtest/factories.py` の H と同じ選定理由)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.backtest.timeframes import floor_to_bucket
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.store import ohlcv as ohlcv_store
from agentic_fx.store import signals
from agentic_fx.store.db import connect, init_db

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
SETTINGS = load_settings(EXAMPLE)
SOURCE = SETTINGS.datafeed.primary  # "yfinance" (既定)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _seed_flat(conn, start: datetime, minutes: int, *, price: float = 100.0,
               source: str = SOURCE, symbol: str = "USDJPY") -> None:
    """producer は `settings.datafeed.primary` (ライブ source) の足を
    読むので、seed も**キャッシュ側**へ書く (プラン 9 Task 16 の分割以降、
    ライブ source を履歴 API へ渡すと allowlist が拒否する)。
    """
    bars = [Bar(symbol, "1m", start + timedelta(minutes=i),
                price, price, price, price, 10.0) for i in range(minutes)]
    ohlcv_store.upsert_cache_bars(conn, bars, source=source)
    first_hour = floor_to_bucket(start, "1h")
    hourly = [Bar(symbol, "1h", first_hour + timedelta(hours=i),
                  price, price, price, price, 10.0)
              for i in range((minutes + 59) // 60)]
    ohlcv_store.upsert_cache_bars(conn, hourly, source=source)


def _meta(*, name: str = "sig", kind: str = "signal", timeframe: str = "1h",
          max_bars: int = 2, pairs: tuple[str, ...] = ("USDJPY",),
          content_hash: str = "h" * 64) -> PluginMeta:
    """fake sandbox_run 経路専用の PluginMeta。path は実在しなくてよい
    (sandbox_run を常に注入し PluginSession を経由しない)。"""
    return PluginMeta(name=name, kind=kind, path=Path("/nonexistent"),
                      params={}, timeframe=timeframe, pairs=pairs,
                      max_bars=max_bars, content_hash=content_hash)


_HOLD = {"action": "hold", "rationale": "no-op", "direction": None,
         "entry_type": None, "limit_price": None, "stop_loss": None,
         "take_profit": None}


def _open_result(**overrides) -> dict:
    base = {"action": "open", "rationale": "go", "direction": "long",
            "entry_type": "market", "limit_price": None,
            "stop_loss": 100.0, "take_profit": None}
    base.update(overrides)
    return base


def _signal_result(**overrides) -> dict:
    base = {"direction": "long", "strength": 0.8, "rationale": "go"}
    base.update(overrides)
    return base


def _resolved_by_identity(meta: PluginMeta) -> dict:
    """[indicator-consumption-wiring] T3 Step 3-2: このファイルの全テストは
    `sandbox_run` を注入する (`_FakeSandbox` が `PluginSession` を経由
    しない) ため `resolved` の値そのものは使われないが、strategy kind の
    meta は `resolved_by_identity` に自分の `(name, content_hash)` が
    無いと未解決として skip される (fail closed)。この helper は
    signal/indicator kind でも無害な空集合エントリを 1 件だけ用意する。"""
    return {(meta.name, meta.content_hash):
            ResolvedIndicatorSet.empty(Path("/nonexistent"))}


def test_market_closure_pauses_once_and_resumes_evaluation(tmp_path, monkeypatch,
                                                            caplog):
    conn = _conn(tmp_path)
    meta = _meta()
    producer = SignalProducer()
    evaluated: list[datetime] = []

    def record_evaluation(*args, now, **kwargs):
        evaluated.append(now)
        return 0

    monkeypatch.setattr(producer, "_evaluate_one", record_evaluation)
    closed = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
    reopened = datetime(2026, 9, 27, 21, 0, 30, tzinfo=timezone.utc)

    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.signal_producer")
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=closed, source=SOURCE,
        sandbox_run=_FakeSandbox(), settings=SETTINGS,
        resolved_by_identity=_resolved_by_identity(meta))
    assert evaluated == []
    assert [record.getMessage() for record in caplog.records] == [
        "market closed — signal evaluation paused until 2026-09-27T21:00:00+00:00"]

    caplog.clear()
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=closed + timedelta(minutes=1), source=SOURCE,
        sandbox_run=_FakeSandbox(), settings=SETTINGS,
        resolved_by_identity=_resolved_by_identity(meta))
    assert caplog.records == []

    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=reopened, source=SOURCE,
        sandbox_run=_FakeSandbox(), settings=SETTINGS,
        resolved_by_identity=_resolved_by_identity(meta))
    assert evaluated == [reopened]
    assert [record.getMessage() for record in caplog.records] == [
        "market open — signal evaluation resumed"]


def _closure_call(producer, conn, meta, now, *, sandbox=None):
    return producer.evaluate_due_plugins(
        conn, plugins=[meta], now=now, source=SOURCE,
        sandbox_run=sandbox if sandbox is not None else _FakeSandbox(),
        settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))


def _levels_and_messages(caplog) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in caplog.records]


def test_market_closure_logs_one_info_per_transition_across_a_week(
        tmp_path, monkeypatch, caplog):
    """閉場入りと開場で INFO が 1 回ずつ出て、開場中の後続 tick は無音、
    次の閉場で再び INFO が出る。閉場中の戻り値は 0。"""
    conn = _conn(tmp_path)
    meta = _meta()
    producer = SignalProducer()
    evaluated: list[datetime] = []

    def record_evaluation(*args, now, **kwargs):
        evaluated.append(now)
        return 0

    monkeypatch.setattr(producer, "_evaluate_one", record_evaluation)
    caplog.set_level(logging.DEBUG, logger="agentic_fx.plugin.signal_producer")

    saturday = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
    assert _closure_call(producer, conn, meta, saturday) == 0
    assert _levels_and_messages(caplog) == [(
        logging.INFO,
        "market closed — signal evaluation paused until 2026-09-27T21:00:00+00:00")]

    caplog.clear()
    reopened = datetime(2026, 9, 27, 21, 0, 30, tzinfo=timezone.utc)
    _closure_call(producer, conn, meta, reopened)
    assert _levels_and_messages(caplog) == [
        (logging.INFO, "market open — signal evaluation resumed")]

    caplog.clear()
    next_tick = reopened + timedelta(minutes=1)
    _closure_call(producer, conn, meta, next_tick)
    assert caplog.records == []
    assert evaluated == [reopened, next_tick]

    friday_close = datetime(2026, 10, 2, 21, 0, 30, tzinfo=timezone.utc)
    assert _closure_call(producer, conn, meta, friday_close) == 0
    assert _levels_and_messages(caplog) == [(
        logging.INFO,
        "market closed — signal evaluation paused until 2026-10-04T21:00:00+00:00")]
    assert evaluated == [reopened, next_tick]


def test_market_closure_first_seen_shortly_before_open_names_that_open(
        tmp_path, monkeypatch, caplog):
    """閉場中の最初の tick が再開 30 分前でも、再開予定は同じ日曜 21:00。"""
    conn = _conn(tmp_path)
    meta = _meta()
    producer = SignalProducer()
    monkeypatch.setattr(producer, "_evaluate_one",
                        lambda *args, **kwargs: 0)
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.signal_producer")

    _closure_call(producer, conn, meta,
                  datetime(2026, 9, 27, 20, 30, tzinfo=timezone.utc))
    assert [record.getMessage() for record in caplog.records] == [
        "market closed — signal evaluation paused until 2026-09-27T21:00:00+00:00"]


def test_market_open_resume_is_logged_before_the_evaluation_warning(
        tmp_path, caplog):
    """開場直後の評価で対象 bucket が無ければ従来どおり WARNING が出るが、
    その前に再開の INFO が出ている。"""
    conn = _conn(tmp_path)
    meta = _meta()
    producer = SignalProducer()
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.signal_producer")

    _closure_call(producer, conn, meta,
                  datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    caplog.clear()
    _closure_call(producer, conn, meta,
                  datetime(2026, 9, 27, 21, 0, 30, tzinfo=timezone.utc))

    levels = [record.levelno for record in caplog.records]
    assert caplog.records[0].getMessage() == "market open — signal evaluation resumed"
    assert levels[0] == logging.INFO
    assert logging.WARNING in levels[1:]


def test_market_closure_is_silent_for_plugins_that_would_be_skipped(
        tmp_path, caplog):
    """閉場中は skip 理由の WARNING (pair が settings 外 / strategy 未解決) も
    出さず、paused の INFO だけを出す。"""
    conn = _conn(tmp_path)
    outside_pair = _meta(name="other_pair", pairs=("EURUSD",))
    unresolved = _meta(name="unresolved", kind="strategy",
                       content_hash="u" * 64)
    producer = SignalProducer()
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.signal_producer")

    for minute in (0, 1):
        producer.evaluate_due_plugins(
            conn, plugins=[outside_pair, unresolved],
            now=datetime(2026, 9, 26, 8, minute, tzinfo=timezone.utc),
            source=SOURCE, sandbox_run=_FakeSandbox(), settings=SETTINGS,
            resolved_by_identity={})
    assert _levels_and_messages(caplog) == [(
        logging.INFO,
        "market closed — signal evaluation paused until 2026-09-27T21:00:00+00:00")]


class _FakeSandbox:
    """`SandboxRunFn` 契約 (meta, payload, *, settings) -> dict の fake。

    plugin 名ごとにキューされた戻り値を順に返す (尽きたら既定値 —
    signal は空リスト・strategy は hold)。`raise_once` で 1 回だけ例外を
    送出させられる (sandbox 失敗のシミュレート)。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._results: dict[str, list] = {}
        self._raises: dict[str, list[Exception]] = {}

    def queue(self, plugin_name: str, *results: dict) -> None:
        self._results.setdefault(plugin_name, []).extend(results)

    def raise_once(self, plugin_name: str, exc: Exception) -> None:
        self._raises.setdefault(plugin_name, []).append(exc)

    def __call__(self, meta: PluginMeta, payload: dict, *, settings) -> dict:
        self.calls.append((meta.name, payload))
        pending_raises = self._raises.get(meta.name)
        if pending_raises:
            raise pending_raises.pop(0)
        q = self._results.get(meta.name)
        if q:
            return q.pop(0)
        if meta.kind == "signal":
            return {"signals": []}
        return dict(_HOLD)


# ---------------------------------------------------------------------
# ⑪ 非分格子 now でバケット進行時に 1 回だけ発火 (opus R2 C2 killer)
# ---------------------------------------------------------------------
def test_fires_once_per_bucket_progression_with_non_minute_aligned_now(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    producer = SignalProducer()

    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, seconds=31),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert len(fake.calls) == 1

    # 同一バケット (floor は依然 H+1:00) の間は非分格子 now で何度呼んでも
    # 追加発火しない
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, seconds=45),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, minutes=30),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert len(fake.calls) == 1

    # バケット進行 (floor が H+2:00 へ進む) で 1 回だけ追加発火
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=2, seconds=31),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------
# ⑫ 同一バケット内の複数 tick で再評価しない (⑪と重複しない単独ピン)
# ---------------------------------------------------------------------
def test_no_reevaluation_within_same_bucket_across_ticks(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 5 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    producer = SignalProducer()

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=1, seconds=31),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    n_after_first = len(fake.calls)
    assert n_after_first > 0

    for delta in (timedelta(minutes=1), timedelta(minutes=30), timedelta(minutes=59)):
        producer.evaluate_due_plugins(
            conn, plugins=[meta], now=H + timedelta(hours=1) + delta,
            source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert len(fake.calls) == n_after_first


# ---------------------------------------------------------------------
# ⑬ 4h plugin は 4h 進行のみ (1h 刻みの tick では発火しない)
# ---------------------------------------------------------------------
def test_4h_plugin_only_fires_on_4h_progression(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=6), 20 * 60 + 1)
    meta = _meta(kind="signal", timeframe="4h")
    fake = _FakeSandbox()
    producer = SignalProducer()

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=4, seconds=31),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    n0 = len(fake.calls)
    assert n0 > 0

    for h in (1, 2, 3):  # 4h 境界に乗らない 1h 刻みの tick
        producer.evaluate_due_plugins(
            conn, plugins=[meta], now=H + timedelta(hours=4 + h),
            source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
        assert len(fake.calls) == n0  # 発火しない

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=8, seconds=31),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert len(fake.calls) == n0 + 1  # 4h 境界進行で 1 回だけ発火


# ---------------------------------------------------------------------
# ⑭ hold 非保存 + hold 後の cursor 喪失 (producer 再生成) → 冪等回復
# ---------------------------------------------------------------------
def test_hold_not_stored_and_survives_cursor_loss_idempotently(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(name="strat", kind="strategy", timeframe="1h")
    fake = _FakeSandbox()  # 既定で hold を返す

    producer1 = SignalProducer()
    inserted1 = producer1.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted1 == 0
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0

    # producer 再生成 = cursor 喪失。鮮度窓内バケットが再評価されるが
    # hold なので行は増えない (冪等)。
    producer2 = SignalProducer()
    inserted2 = producer2.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted2 == 0
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0
    assert len(fake.calls) > 0  # 実際に再評価は起きている (cursor が空だった証拠)


# ---------------------------------------------------------------------
# ⑮ 2 バケット超の停止 → 鮮度窓内のみ catch-up・窓外は評価しない
# ---------------------------------------------------------------------
def test_stall_beyond_window_only_catches_up_within_freshness(tmp_path):
    assert SETTINGS.plugin.signal_freshness_bars == 2  # 前提のピン
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=2), 13 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    producer = SignalProducer()

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=1, seconds=31),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    n0 = len(fake.calls)
    assert n0 == 1  # 鮮度窓は grace で広がらない

    # 9 時間分の停止を模す (9 バケット経過、鮮度窓 2 バケットを大きく超える)
    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=10, seconds=31),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert len(fake.calls) == n0 + 1  # 窓内 1 バケットのみ追加評価・窓外は評価しない


def test_freshness_window_does_not_expand_by_closed_bar_grace(tmp_path):
    """grace は close 判定専用で、signal_freshness_bars を広げない。"""
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    producer = SignalProducer()

    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, seconds=31),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS,
        resolved_by_identity=_resolved_by_identity(meta))

    assert len(fake.calls) == 1


# ---------------------------------------------------------------------
# ⑯ sandbox 失敗 → cursor が進まず次 tick で同一バケットを再試行
# ---------------------------------------------------------------------
def test_sandbox_failure_does_not_advance_cursor(tmp_path, caplog):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    fake.raise_once("sig", SandboxError("plugin crashed"))
    producer = SignalProducer()

    with caplog.at_level(logging.WARNING, logger="agentic_fx.plugin.signal_producer"):
        inserted = producer.evaluate_due_plugins(
            conn, plugins=[meta], now=H + timedelta(hours=1),
            source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted == 0
    assert "evaluation failed" in caplog.text
    n_after_failure = len(fake.calls)

    # 次 tick (同じ now) で同じバケットを再試行する — 今度は成功させる
    fake.queue("sig", {"signals": [_signal_result()]})
    inserted2 = producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted2 == 1
    assert len(fake.calls) > n_after_failure


# ---------------------------------------------------------------------
# ⑰ pairs 積集合外スキップ
# ---------------------------------------------------------------------
def test_pair_outside_settings_pairs_is_skipped_with_warning(tmp_path, caplog):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1, symbol="USDJPY")
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1, symbol="EURUSD")
    meta = _meta(kind="signal", timeframe="1h", pairs=("USDJPY", "EURUSD"))
    fake = _FakeSandbox()
    producer = SignalProducer()

    with caplog.at_level(logging.WARNING, logger="agentic_fx.plugin.signal_producer"):
        producer.evaluate_due_plugins(
                conn, plugins=[meta], now=H + timedelta(hours=1),
            source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))

    # settings.pairs (= ["USDJPY"]) に無い EURUSD は評価対象から除外される。
    # 呼び出し回数は USDJPY 分 (鮮度窓 2 バケット) のみのはず。
    assert len(fake.calls) == 1
    assert "EURUSD" in caplog.text
    assert "settings.pairs" in caplog.text


# ---------------------------------------------------------------------
# ⑱ dedupe: cursor 喪失後の再評価でも signals 行は重複しない
# ---------------------------------------------------------------------
def test_dedupe_on_cursor_loss_reevaluation(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    sig = _signal_result()

    producer1 = SignalProducer()
    fake.queue("sig", {"signals": [sig]}, {"signals": [sig]})
    inserted1 = producer1.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted1 >= 1
    count_after_first = conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"]

    # producer 再生成 (cursor 喪失) — 同じバケットが再評価され、同じ内容の
    # signal が返っても dedupe (UNIQUE キー) で行は増えない。
    producer2 = SignalProducer()
    fake.queue("sig", {"signals": [sig]}, {"signals": [sig]})
    inserted2 = producer2.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted2 == 0
    count_after_second = conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"]
    assert count_after_second == count_after_first


# ---------------------------------------------------------------------
# ⑲ row の bar_ts が評価バケット開始時刻に強制される
# ---------------------------------------------------------------------
def test_row_bar_ts_is_forced_to_bucket_start(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    fake.queue("sig", {"signals": [_signal_result()]})
    now = H + timedelta(hours=1)
    producer = SignalProducer()
    producer.evaluate_due_plugins(conn, plugins=[meta], now=now, source=SOURCE,
                                  sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))

    rows = conn.execute("SELECT bar_ts FROM signals").fetchall()
    assert len(rows) == 1
    bar_ts = datetime.fromisoformat(rows[0]["bar_ts"])
    assert bar_ts.tzinfo is not None and bar_ts.utcoffset() == timedelta(0)
    assert bar_ts < now  # 未来値でない
    assert bar_ts == floor_to_bucket(bar_ts, "1h")  # バケット開始時刻そのもの


# ---------------------------------------------------------------------
# action="open" のみ保存されること (⑭の裏)
# ---------------------------------------------------------------------
def test_strategy_open_action_is_stored(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(name="strat", kind="strategy", timeframe="1h")
    fake = _FakeSandbox()
    fake.queue("strat", _open_result())
    producer = SignalProducer()
    inserted = producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1), source=SOURCE,
        sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted == 1
    row = conn.execute("SELECT * FROM signals").fetchone()
    assert row["kind"] == "strategy"


# ---------------------------------------------------------------------
# signal/strategy 以外の kind (indicator) は無視される
# ---------------------------------------------------------------------
def test_indicator_plugins_are_ignored(tmp_path):
    conn = _conn(tmp_path)
    meta = PluginMeta(name="ind", kind="indicator", path=Path("/nonexistent"),
                      params={}, timeframe=None, pairs=(), max_bars=50,
                      content_hash="h" * 64)
    fake = _FakeSandbox()
    producer = SignalProducer()
    inserted = producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1), source=SOURCE,
        sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))
    assert inserted == 0
    assert fake.calls == []


# ---------------------------------------------------------------------
# fix round 1 F3 (codex): df が非空でも対象バケットの行が無ければ
# (取り込みラグ) 評価完了と誤認しない — 偽の signal を生成せず、cursor も
# 進めない。データが後から到着すれば同じバケットが再評価される。
# ---------------------------------------------------------------------
def test_missing_target_bucket_data_is_not_mistaken_for_completion(tmp_path, caplog):
    conn = _conn(tmp_path)
    # データは H (排他) まで — 対象バケット [H, H+1h) の 1m 行は 1 本も無い
    # (取り込みラグを模す)。バケット [H-1h, H) には十分なデータがある。
    _seed_flat(conn, H - timedelta(hours=3), 3 * 60)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    fake.queue("sig", {"signals": [_signal_result()]})  # H-1:00 用の 1 件のみ
    producer = SignalProducer()

    with caplog.at_level(logging.WARNING, logger="agentic_fx.plugin.signal_producer"):
        producer.evaluate_due_plugins(
                conn, plugins=[meta], now=H + timedelta(hours=1),
            source=SOURCE, sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))

    # H-1:00 は評価され signal 化されるが、対象バケット H:00 は不在として
    # fail-open されるため signal は生成されない (bar_ts=H:00 の行が無い)。
    rows = conn.execute("SELECT bar_ts FROM signals").fetchall()
    assert [r["bar_ts"] for r in rows] == [(H - timedelta(hours=1)).isoformat()]
    assert "not yet present" in caplog.text

    # 後からバーが到着 (取り込みラグ解消) — cursor が進んでいないので同じ
    # バケットが次 tick (同じ now) で再評価される。
    _seed_flat(conn, H, 60)
    fake.queue("sig", {"signals": [_signal_result()]})
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, seconds=31), source=SOURCE,
        sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))

    rows2 = conn.execute("SELECT bar_ts FROM signals ORDER BY bar_ts").fetchall()
    assert [r["bar_ts"] for r in rows2] == [
        (H - timedelta(hours=1)).isoformat(), H.isoformat()]


# ---------------------------------------------------------------------
# fix round 1 F4 (codex): 同一バケットで detect が複数出力を返すと UNIQUE
# (plugin, content_hash, pair, timeframe, bar_ts) により 2 個目以降が
# INSERT OR IGNORE で音もなく消える (DDL は §12 逐語のため変更しない —
# plan 由来の制約) — 2 個目以降の drop だけは warning で観測可能にする。
# ---------------------------------------------------------------------
def test_multiple_signals_in_same_bucket_drop_is_observed_via_warning(
        tmp_path, caplog):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    # H-1:00 のバケットだけ 2 出力を積む (H:00 は既定の空リストのまま)
    fake.queue("sig", {"signals": [_signal_result(direction="long"),
                                   _signal_result(direction="short")]})
    producer = SignalProducer()

    with caplog.at_level(logging.WARNING, logger="agentic_fx.plugin.signal_producer"):
        inserted = producer.evaluate_due_plugins(
            conn, plugins=[meta], now=H + timedelta(hours=1), source=SOURCE,
            sandbox_run=fake, settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta))

    assert inserted == 1  # 2 出力中 1 個だけ挿入される
    count = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
    assert count == 1
    assert "dropped by" in caplog.text
    assert "UNIQUE" in caplog.text


def test_short_window_is_recorded_once_until_the_state_changes(tmp_path):
    """要求 max_bars に満たない窓しか読めなかったら loud に知らせる。

    `load_resampled_frame(..., max_bars=N)` は「在る分だけ」を返す。
    キャッシュ保持期間 (`datafeed.cache_retention_days`) が plugin の
    max_bars が要求する窓より短いと、末尾バケットは存在するので
    `_evaluate_bucket` の fail-open 分岐に入らず、**切り詰められた系列で
    指標が計算され signal がそのまま出る**。承認時は削除されない履歴
    テーブルで評価するため、この劣化は本番でしか現れない。
    """
    conn = _conn(tmp_path)
    # 1h plugin が 50 本要求するのに 3 バケット分しか無い状態
    _seed_flat(conn, H - timedelta(hours=3), 3 * 60 + 1)
    meta = _meta(name="short", kind="signal", timeframe="1h", max_bars=50)
    fake = _FakeSandbox()
    fake.queue("short", {"signals": []})
    producer = SignalProducer()
    class RecordingActivity:
        def __init__(self):
            self.calls = []

        def write(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    activity = RecordingActivity()
    for now in (H + timedelta(hours=1), H + timedelta(hours=2)):
        producer.evaluate_due_plugins(
            conn, plugins=[meta], now=now, source=SOURCE,
            sandbox_run=fake, settings=SETTINGS,
            resolved_by_identity=_resolved_by_identity(meta), activity=activity)
    assert len(activity.calls) == 1
    assert activity.calls[0][0][1] == "plugin_insufficient_closed_bars"


def test_signal_producer_module_does_not_read_settings_backtest():
    """A1 (設計 v3): ライブ signal_producer は adapter を使わず
    `load_resampled_frame` を直接呼ぶ → 明示 base_interval="1m" を渡す
    契約であり、`settings.backtest` (バックテスト専用の dataset/eval_source
    設定) を読んではならない (pin)。ソース走査で `settings.backtest` への
    アクセスが一切無いことを固定する — バックテスト設定の変更がライブ
    producer の挙動に波及する経路が生まれていないことの回帰防止。"""
    import inspect

    from agentic_fx.plugin import signal_producer

    src = inspect.getsource(signal_producer)
    assert "settings.backtest" not in src


# ---------------------------------------------------------------------
# [indicator-consumption-wiring] T3 Step 3-2 (F2): resolved_by_identity
# ---------------------------------------------------------------------

def test_producer_requires_resolved_for_strategy_and_reuses_the_same_object(
        tmp_path, monkeypatch):
    """F2: producer は解決済み strategy しか評価せず、
    `InventoryBuildResult.resolved` が持つ `ResolvedIndicatorSet` を
    **そのまま** session へ渡す (再解決しない)。sandbox_run を注入しない
    経路 (実 `PluginSession` は monkeypatch で spy に差し替える)。"""
    from agentic_fx.plugin import sandbox as plugin_sandbox

    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    strategy_meta = _meta(name="strat", kind="strategy", timeframe="1h")
    seen = {}

    class _Spy:
        def __init__(self, meta, *, settings, resolved=None):
            seen[meta.content_hash] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            return dict(_HOLD)
        def close(self):
            return None

    monkeypatch.setattr(plugin_sandbox, "PluginSession", _Spy)
    sentinel = ResolvedIndicatorSet.empty(tmp_path)
    producer = SignalProducer()
    producer.evaluate_due_plugins(
        conn, plugins=[strategy_meta], now=H + timedelta(hours=1), source=SOURCE,
        settings=SETTINGS,
        resolved_by_identity={(strategy_meta.name, strategy_meta.content_hash):
                              sentinel})
    assert seen[strategy_meta.content_hash] is sentinel


def test_producer_skips_strategy_without_resolution(tmp_path, caplog):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    strategy_meta = _meta(name="strat", kind="strategy", timeframe="1h")
    producer = SignalProducer()
    with caplog.at_level(logging.WARNING,
                         logger="agentic_fx.plugin.signal_producer"):
        inserted = producer.evaluate_due_plugins(
            conn, plugins=[strategy_meta], now=H + timedelta(hours=1),
            source=SOURCE, settings=SETTINGS, resolved_by_identity={})
    assert inserted == 0
    assert "unresolved" in caplog.text


def test_producer_uses_name_and_hash_identity_not_hash_alone(
        tmp_path, monkeypatch):
    """P3' 回帰 (codex plan r2 束2 Critical): 同一 `content_hash`・別名の 2
    strategy が同時に inventory に存在するとき、producer は
    `InventoryBuildResult.resolved` の `(name, content_hash)` キーで
    `resolved_by_identity` を引く — `content_hash` だけをキーにした辞書へ
    潰すと (`.get(meta.content_hash)`)、どちらの meta にも一致するキーが
    無くなり `seen["strat_a"]` が `None` になる (この assert がその変異を
    殺す、実測で確認済み)。

    [indicator-consumption-wiring] T3 Step 3-2c の docstring どおり、
    **同一 content_hash の 2 strategy は同じバッチ内では session cache
    (`content_hash` 単独キー) を共有する** — 2 つ目の meta では
    `PluginSession(...)` が再構築されないため、2 つ目の `resolved` は
    そもそも観測できない (plan Step 3-2a の原案は `seen["strat_b"]` も
    直接 assert していたが、これは Step 3-2c 自身の session 共有設計と
    両立しない — 実測で `KeyError` を確認したため、ここでは「session は
    1 個だけ・最初の meta の正しい resolved を受け取る」ことと「2 つ目の
    meta も skip されず実際に評価される (session 共有経由)」ことを
    assert する形に直した。2 つ目の meta 単独の resolved 受け渡しは
    別呼び出し (下) で確認する。"""
    import dataclasses

    from agentic_fx.plugin import sandbox as plugin_sandbox

    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    shared_hash = "b" * 64
    base_meta = _meta(name="strat_a", kind="strategy", timeframe="1h",
                      content_hash=shared_hash)
    meta_a = base_meta
    meta_b = dataclasses.replace(base_meta, name="strat_b")
    seen = {}
    call_count = {"n": 0}

    class _Spy:
        def __init__(self, meta, *, settings, resolved=None):
            seen[meta.name] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            call_count["n"] += 1
            return dict(_HOLD)
        def close(self):
            return None

    monkeypatch.setattr(plugin_sandbox, "PluginSession", _Spy)
    sentinel_a = ResolvedIndicatorSet.empty(tmp_path / "a")
    sentinel_b = ResolvedIndicatorSet.empty(tmp_path / "b")
    producer = SignalProducer()
    producer.evaluate_due_plugins(
        conn, plugins=[meta_a, meta_b], now=H + timedelta(hours=1, seconds=31),
        source=SOURCE, settings=SETTINGS,
        resolved_by_identity={
            ("strat_a", shared_hash): sentinel_a,
            ("strat_b", shared_hash): sentinel_b})
    # 唯一構築された session (meta_a 分) は自分自身の (name, hash) に
    # 一致する resolved を受け取る — content_hash だけで潰した辞書だと
    # ここが None になる (段 0 逆変異 (d) の観測点)。
    assert seen == {"strat_a": sentinel_a}
    # meta_b は「未解決として skip」されたのではなく、共有 session 経由で
    # 実際に評価されている (skip なら call_count は meta_a 分の 2 回のまま)。
    assert call_count["n"] == 2  # 鮮度窓 1 バケット × 2 meta

    # 2 つ目の meta 単独では、別バッチ (別 session) で自分自身の resolved
    # を正しく受け取る。
    seen.clear()
    producer2 = SignalProducer()
    producer2.evaluate_due_plugins(
        conn, plugins=[meta_b], now=H + timedelta(hours=1),
        source=SOURCE, settings=SETTINGS,
        resolved_by_identity={("strat_b", shared_hash): sentinel_b})
    assert seen == {"strat_b": sentinel_b}


def test_producer_looks_up_resolved_by_key_not_by_dict_position(
        tmp_path, monkeypatch):
    """段 0 束 2 M11 (SURVIVED) の pin: `resolved_by_identity` は
    **キーで引く** — 辞書の先頭要素を無条件に使う変異
    (`next(iter(resolved_by_identity.values()), None)`) を殺す。

    既存の `test_producer_uses_name_and_hash_identity_not_hash_alone` は
    評価対象 meta の resolved が**辞書の先頭**に置かれた fixture だった
    ため、「位置で引く」変異と「キーで引く」実装を区別できなかった。
    ここでは**無関係な strategy の resolved を先頭**に置き、評価する
    meta の resolved を後ろに置く。"""
    import dataclasses

    from agentic_fx.plugin import sandbox as plugin_sandbox

    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    hash_a = "a" * 64
    hash_other = "c" * 64
    meta_a = _meta(name="strat_a", kind="strategy", timeframe="1h",
                   content_hash=hash_a)
    meta_other = dataclasses.replace(meta_a, name="strat_other",
                                     content_hash=hash_other)
    seen = {}

    class _Spy:
        def __init__(self, meta, *, settings, resolved=None):
            seen[meta.name] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            return dict(_HOLD)
        def close(self):
            return None

    monkeypatch.setattr(plugin_sandbox, "PluginSession", _Spy)
    sentinel_a = ResolvedIndicatorSet.empty(tmp_path / "a")
    sentinel_other = ResolvedIndicatorSet.empty(tmp_path / "other")
    assert sentinel_a != sentinel_other
    producer = SignalProducer()
    producer.evaluate_due_plugins(
        conn, plugins=[meta_a], now=H + timedelta(hours=1),
        source=SOURCE, settings=SETTINGS,
        resolved_by_identity={            # ← 先頭は評価しない meta のもの
            ("strat_other", hash_other): sentinel_other,
            ("strat_a", hash_a): sentinel_a,
        })
    assert seen == {"strat_a": sentinel_a}


# ---------------------------------------------------------------------
# live の評価失敗 activity (固定分類・間引き・解除)
# ---------------------------------------------------------------------
import pytest  # noqa: E402

_FAILURE_CASES = [
    pytest.param(SandboxError("x", code="timeout"), "timeout", id="timeout"),
    pytest.param(SandboxError("x", code="cpu_limit"), "cpu_limit", id="cpu_limit"),
    pytest.param(SandboxError("x", code="crashed"), "crashed", id="crashed"),
    pytest.param(SandboxError("x", code="plugin_error"), "plugin_error",
                 id="plugin_error"),
    pytest.param(SandboxError("x", code="protocol_error"), "plugin_error",
                 id="protocol_error"),
    pytest.param(SandboxError("x"), "plugin_error", id="default_code"),
    pytest.param(RuntimeError("x"), "internal_error", id="non_sandbox"),
]


class _RecordingActivity:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def write(self, category, event, summary, ref_id=None) -> None:
        self.calls.append((category, event, summary, ref_id))

    def failures(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "plugin_eval_failed"]


class _AlwaysFailingSandbox:
    """呼ばれるたびに指定の例外を送出し、評価したバケット (df 末尾) を残す。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.buckets: list[datetime] = []

    def __call__(self, meta, payload, *, settings):
        self.buckets.append(payload["df"].index[-1].to_pydatetime())
        raise self.exc


def _run(producer, conn, meta, now, sandbox, activity):
    return producer.evaluate_due_plugins(
        conn, plugins=[meta], now=now, source=SOURCE, sandbox_run=sandbox,
        settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta),
        activity=activity)


@pytest.mark.parametrize("exc, result", _FAILURE_CASES)
def test_live_failure_keeps_cursor_breaks_the_tick_and_reports_the_fixed_class(
        tmp_path, exc, result):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=8), 8 * 60 + 61)
    meta = _meta(kind="signal", timeframe="1h")
    sandbox = _AlwaysFailingSandbox(exc)
    activity = _RecordingActivity()
    producer = SignalProducer()
    now = H + timedelta(hours=1)

    assert _run(producer, conn, meta, now, sandbox, activity) == 0
    # 同 tick は最初に失敗したバケットで打ち切る (後続バケットを評価しない)
    assert len(sandbox.buckets) == 1
    # cursor は進まず、次 tick は同じバケットから再試行する
    assert _run(producer, conn, meta, now, sandbox, activity) == 0
    assert sandbox.buckets[1] == sandbox.buckets[0]
    assert len(sandbox.buckets) == 2
    # 通知は初回の 1 件だけ (2 回目は間引き)、本文は固定分類
    assert len(activity.failures()) == 1
    assert f"result={result}" in activity.failures()[0]
    assert activity.calls[0][3] == "sig"


@pytest.mark.parametrize("exc, result", _FAILURE_CASES)
def test_live_failure_uses_a_new_worker_every_tick(tmp_path, exc, result):
    from unittest.mock import patch

    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    sessions: list = []

    class _Session:
        def __init__(self, m, *, settings, resolved=None) -> None:
            self.pid = 1000 + len(sessions)
            self.closed = False
            sessions.append(self)

        def __enter__(self):
            return self

        def call(self, payload):
            raise exc

        def close(self) -> None:
            self.closed = True

    producer = SignalProducer()
    activity = _RecordingActivity()
    with patch("agentic_fx.plugin.signal_producer.plugin_sandbox.PluginSession",
               _Session):
        for _ in range(2):
            _run(producer, conn, meta, H + timedelta(hours=1), None, activity)
    assert [s.pid for s in sessions] == [1000, 1001]
    assert all(s.closed for s in sessions)
    assert f"result={result}" in activity.failures()[0]


@pytest.mark.parametrize("exc, result", _FAILURE_CASES)
def test_live_failure_notifies_only_on_attempts_1_61_121(tmp_path, exc, result,
                                                          caplog):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    sandbox = _AlwaysFailingSandbox(exc)
    producer = SignalProducer()
    now = H + timedelta(hours=1)

    notified: list[int] = []
    warned: list[int] = []
    caplog.set_level(logging.WARNING, logger="agentic_fx.plugin.signal_producer")
    for attempt in range(1, 122):
        activity = _RecordingActivity()
        caplog.clear()
        _run(producer, conn, meta, now, sandbox, activity)
        if activity.failures():
            notified.append(attempt)
            assert f"result={result}" in activity.failures()[0]
            assert ("suppressed_count=60" in activity.failures()[0]) == (attempt > 1)
        if "evaluation failed" in caplog.text:
            warned.append(attempt)
    assert notified == [1, 61, 121]
    assert warned == [1, 61, 121]


@pytest.mark.parametrize("exc, result", _FAILURE_CASES)
def test_live_failure_key_is_released_after_the_hash_changes_back(
        tmp_path, exc, result):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    old = _meta(kind="signal", timeframe="1h", content_hash="a" * 64)
    new = _meta(kind="signal", timeframe="1h", content_hash="b" * 64)
    sandbox = _AlwaysFailingSandbox(exc)
    activity = _RecordingActivity()
    producer = SignalProducer()
    now = H + timedelta(hours=1)

    _run(producer, conn, old, now, sandbox, activity)
    _run(producer, conn, old, now, sandbox, activity)
    assert len(activity.failures()) == 1
    _run(producer, conn, new, now, sandbox, activity)  # hash 変更 (別 key で初回)
    assert len(activity.failures()) == 2
    _run(producer, conn, old, now, sandbox, activity)  # 戻しても初回扱い
    assert len(activity.failures()) == 3


@pytest.mark.parametrize("exc, result", _FAILURE_CASES)
def test_live_failure_key_is_released_after_a_success(tmp_path, exc, result):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    fake = _FakeSandbox()
    activity = _RecordingActivity()
    producer = SignalProducer()
    now = H + timedelta(hours=1)
    key = ("sig", meta.content_hash, "USDJPY")

    fake.raise_once("sig", exc)
    _run(producer, conn, meta, now, fake, activity)
    fake.raise_once("sig", exc)
    _run(producer, conn, meta, now, fake, activity)
    assert len(activity.failures()) == 1
    _run(producer, conn, meta, now, fake, activity)  # 成功して追いつく
    assert key in producer._cursor
    # 再起動以外で同じバケットが再び失敗する状況 (cursor を戻して再現)
    producer._cursor.pop(key)
    fake.raise_once("sig", exc)
    _run(producer, conn, meta, now, fake, activity)
    assert len(activity.failures()) == 2


@pytest.mark.parametrize("exc, result", _FAILURE_CASES)
def test_live_failure_key_is_released_when_the_bucket_is_abandoned(
        tmp_path, exc, result):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 40 * 60)
    meta = _meta(kind="signal", timeframe="1h")
    sandbox = _AlwaysFailingSandbox(exc)
    activity = _RecordingActivity()
    producer = SignalProducer()

    _run(producer, conn, meta, H + timedelta(hours=1), sandbox, activity)
    assert len(producer._failure_notices) == 1
    # 鮮度窓を過ぎて失敗バケットが自然放棄されたら key も消える
    far = H + timedelta(hours=1 + SETTINGS.plugin.signal_freshness_bars + 2)
    old_bucket = next(iter(producer._failure_notices))[3]
    # 放棄後の最初の確定バケットも失敗させ、成功による解除と区別する
    _run(producer, conn, meta, far, _AlwaysFailingSandbox(exc), activity)
    assert all(k[3] != old_bucket for k in producer._failure_notices)
    assert len(producer._failure_notices) <= 1


def test_live_failure_activity_text_has_no_exception_text_or_path(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")
    sandbox = _AlwaysFailingSandbox(SandboxError(
        "/home/x/secret.py line 3 Traceback STDERR-MARKER", code="crashed"))
    activity = _RecordingActivity()
    _run(SignalProducer(), conn, meta, H + timedelta(hours=1), sandbox, activity)
    text = " ".join(str(part) for call in activity.calls for part in call)
    assert "STDERR-MARKER" not in text and "/home/x" not in text
    assert "Traceback" not in text
    assert "result=crashed" in text


def test_live_missing_bucket_data_is_not_reported_as_a_plugin_failure(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 3 * 60)
    meta = _meta(kind="signal", timeframe="1h")
    activity = _RecordingActivity()
    _run(SignalProducer(), conn, meta, H + timedelta(hours=1), _FakeSandbox(),
         activity)
    assert activity.failures() == []


def test_live_failure_activity_write_error_does_not_stop_the_producer(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    meta = _meta(kind="signal", timeframe="1h")

    class _Broken:
        def write(self, *a, **k):
            raise OSError("disk full")

    fake = _FakeSandbox()
    fake.raise_once("sig", SandboxError("x", code="crashed"))
    producer = SignalProducer()
    now = H + timedelta(hours=1)
    assert _run(producer, conn, meta, now, fake, _Broken()) == 0
    fake.queue("sig", {"signals": [_signal_result()]})
    assert _run(producer, conn, meta, now, fake, _Broken()) == 1

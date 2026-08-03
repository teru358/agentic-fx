"""signal_producer.SignalProducer のテスト (プラン 7 Task 8)。

sandbox サブプロセスは一切起動しない — 全テストで `sandbox_run` に fake を
注入する (`_FakeSandbox`)。実 HTTP/git/乱数/実時刻取得も使わない。

`H` は 2026-07-22T12:00 UTC (epoch 錨に対し 1h/4h いずれの境界にも整列 —
`tests/backtest/conftest.py` の H と同じ選定理由)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.backtest.timeframes import floor_to_bucket
from agentic_fx.config import load_settings
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.store import ohlcv as ohlcv_store
from agentic_fx.store import signals
from agentic_fx.store.db import connect, init_db

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
SETTINGS = load_settings(EXAMPLE)
SOURCE = SETTINGS.plugin.producer_source  # "yfinance" (既定)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _seed_flat(conn, start: datetime, minutes: int, *, price: float = 100.0,
               source: str = SOURCE, symbol: str = "USDJPY") -> None:
    rows = [(symbol, "1m", (start + timedelta(minutes=i)).isoformat(),
             price, price, price, price, 10.0, None) for i in range(minutes)]
    ohlcv_store.import_bars(conn, rows, source=source)


def _meta(*, name: str = "sig", kind: str = "signal", timeframe: str = "1h",
          max_bars: int = 50, pairs: tuple[str, ...] = ("USDJPY",),
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
        conn, plugins=[meta], now=H + timedelta(hours=1, seconds=3),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    assert len(fake.calls) == 1  # bucket H:00 のみ (H-1:00 は鮮度窓外)

    # 同一バケット (floor は依然 H+1:00) の間は非分格子 now で何度呼んでも
    # 追加発火しない
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, seconds=45),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1, minutes=30),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    assert len(fake.calls) == 1

    # バケット進行 (floor が H+2:00 へ進む) で 1 回だけ追加発火
    producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=2, seconds=10),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
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

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=1),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    n_after_first = len(fake.calls)
    assert n_after_first > 0

    for delta in (timedelta(minutes=1), timedelta(minutes=30), timedelta(minutes=59)):
        producer.evaluate_due_plugins(
            conn, plugins=[meta], now=H + timedelta(hours=1) + delta,
            source=SOURCE, sandbox_run=fake, settings=SETTINGS)
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

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=4),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    n0 = len(fake.calls)
    assert n0 > 0

    for h in (1, 2, 3):  # 4h 境界に乗らない 1h 刻みの tick
        producer.evaluate_due_plugins(
            conn, plugins=[meta], now=H + timedelta(hours=4 + h),
            source=SOURCE, sandbox_run=fake, settings=SETTINGS)
        assert len(fake.calls) == n0  # 発火しない

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=8),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS)
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
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    assert inserted1 == 0
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0

    # producer 再生成 = cursor 喪失。鮮度窓内バケットが再評価されるが
    # hold なので行は増えない (冪等)。
    producer2 = SignalProducer()
    inserted2 = producer2.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
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

    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=1),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    n0 = len(fake.calls)
    assert n0 == 2  # 鮮度窓 (2 バケット) 分だけ catch-up

    # 9 時間分の停止を模す (9 バケット経過、鮮度窓 2 バケットを大きく超える)
    producer.evaluate_due_plugins(conn, plugins=[meta], now=H + timedelta(hours=10),
                                  source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    assert len(fake.calls) == n0 + 2  # 窓内 2 バケットのみ追加評価・窓外 7 は評価しない


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
            source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    assert inserted == 0
    assert "evaluation failed" in caplog.text
    n_after_failure = len(fake.calls)

    # 次 tick (同じ now) で同じバケットを再試行する — 今度は成功させる
    fake.queue("sig", {"signals": [_signal_result()]})
    inserted2 = producer.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
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
            source=SOURCE, sandbox_run=fake, settings=SETTINGS)

    # settings.pairs (= ["USDJPY"]) に無い EURUSD は評価対象から除外される。
    # 呼び出し回数は USDJPY 分 (鮮度窓 2 バケット) のみのはず。
    assert len(fake.calls) == 2
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
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
    assert inserted1 >= 1
    count_after_first = conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"]

    # producer 再生成 (cursor 喪失) — 同じバケットが再評価され、同じ内容の
    # signal が返っても dedupe (UNIQUE キー) で行は増えない。
    producer2 = SignalProducer()
    fake.queue("sig", {"signals": [sig]}, {"signals": [sig]})
    inserted2 = producer2.evaluate_due_plugins(
        conn, plugins=[meta], now=H + timedelta(hours=1),
        source=SOURCE, sandbox_run=fake, settings=SETTINGS)
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
                                  sandbox_run=fake, settings=SETTINGS)

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
        sandbox_run=fake, settings=SETTINGS)
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
        sandbox_run=fake, settings=SETTINGS)
    assert inserted == 0
    assert fake.calls == []

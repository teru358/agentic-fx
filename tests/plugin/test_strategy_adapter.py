"""strategy_adapter.py のテスト (プラン 7 Task 5)。

fake session 注入でサンドボックス実プロセスを回避する (①〜④・close/lazy
生成系)。唯一の例外は⑤ (統合テスト) — Task 2 の tests/plugin/test_sandbox.py
に実サブプロセスの先例があるサンプル plugin (sma_cross) を使い、実
`PluginSession` で 5 時間 replay して intent が orders に到達すること・
セッションが 1 プロセスのみで使い回されたこと (pid・Popen 呼び出し回数)
を確認する。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentic_fx.backtest.runner import run_replay
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin.loader import PluginMeta, content_hash as _real_content_hash
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.store import ohlcv as ohlcv_store
from tests.backtest.factories import H, SETTINGS, _conn, _row_at, DATASET_1M

SMA_CROSS_DIR = (Path(__file__).resolve().parents[2] / "docs" / "examples"
                 / "plugins" / "sma_cross")

# [indicator-consumption-wiring] T3 Step 3-1: `resolved` はキーワード必須。
# 依存 0 本のテストではこの空集合を渡す。
_EMPTY = ResolvedIndicatorSet.empty(Path("/nonexistent/plugins"))


def _meta(*, name: str = "strat", timeframe: str = "4h", max_bars: int = 200,
         pairs: tuple[str, ...] = ("USDJPY",), params: dict | None = None) -> PluginMeta:
    """fake session 経路のテスト用 PluginMeta。path は実在しない —
    fake session を注入する限り PluginSession/loader を経由しないので
    ディスク上に plugin フォルダが存在する必要はない。"""
    return PluginMeta(name=name, kind="strategy", path=Path("/nonexistent"),
                      params=params or {}, timeframe=timeframe, pairs=pairs,
                      max_bars=max_bars, content_hash="deadbeef" * 8)


def _bar(ts, *, interval: str = "1h") -> Bar:
    return Bar(symbol="USDJPY", interval=interval, ts=ts, open=100.0,
              high=100.0, low=100.0, close=100.0, volume=1.0)


def _seed_flat(conn, start, minutes: int, *, price: float = 100.0,
               source: str = "dukascopy") -> None:
    rows = [_row_at(start + timedelta(minutes=i), o=price, h=price, l=price,
                    c=price) for i in range(minutes)]
    ohlcv_store.import_history_bars(conn, rows, source=source)


_HOLD_RESULT = {"action": "hold", "rationale": "no-op", "direction": None,
                "entry_type": None, "limit_price": None, "stop_loss": None,
                "take_profit": None}


class _FakeSession:
    """`.call()` の呼び出し payload を記録するだけの fake session。
    `results` を順に返す (尽きたら hold を返す)。`raises` を渡すと
    `.call()` がそれをそのまま送出する (SandboxError 貫通テスト用)。
    `.close()` は呼び出しを記録するだけ (session 注入シームの所有権テスト用)。
    """

    def __init__(self, results: list[dict] | None = None, *,
                raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._results = list(results) if results is not None else []
        self._raises = raises
        self.closed = False

    def call(self, payload: dict) -> dict:
        self.calls.append(payload)
        if self._raises is not None:
            raise self._raises
        if self._results:
            return self._results.pop(0)
        return dict(_HOLD_RESULT)

    def close(self) -> None:
        self.closed = True


# --- ①発火条件: 1h 評価格子で 4h plugin は 4h 境界終端の bar のみ評価 ------

def test_fires_only_on_declared_timeframe_boundary(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="4h")
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)

    fired_at = []
    for i in range(8):  # closed_bar ts = H, H+1h, ..., H+7h (1h 足 8 本)
        before = len(session.calls)
        src(_bar(H + timedelta(hours=i)))
        if len(session.calls) > before:
            fired_at.append(i)

    # bucket_end = closed_bar.ts + 1h が 4h 境界 (epoch 錨) と一致するのは
    # i=3 (bucket_end=H+4h) と i=7 (bucket_end=H+8h) のみ。
    assert fired_at == [3, 7]
    # F4 (sonnet Minor — レビュー fix round 1): eval_count += 1 を
    # __call__ 先頭へ移す変異 (発火判定を経ずに毎回カウントする) が
    # fired_at の検証だけでは生存し得る — eval_count を直接ピンする。
    assert src.eval_count == 2


def test_fires_only_on_1h_boundary_with_30m_eval_grid(tmp_path):
    """F1 (codex Medium — レビュー fix round 1): `closed_bar.interval` が
    `TF_MINUTES` に無い任意の `run_replay` 合法値 (30m 等 — runner.py の
    `parse_timeframe` が受理する任意の "Nm"/"Nh") でも幅導出が機能する
    ことを確認する。旧実装 (`TF_MINUTES` 参照) はこの eval_timeframe で
    必ず `ValueError` になっていた — この RED が変異証明を兼ねる。"""
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 4 * 60 + 1)
    meta = _meta(timeframe="1h")
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)

    fired_at = []
    for i in range(8):  # closed_bar ts = H, H+30m, ..., H+3h30m (30m 足 8 本)
        before = len(session.calls)
        src(_bar(H + timedelta(minutes=30 * i), interval="30m"))
        if len(session.calls) > before:
            fired_at.append(i)

    # bucket_end = closed_bar.ts + 30m が 1h 境界と一致するのは奇数 i のみ
    # (i=1: 12:30+30m=13:00, i=3: 14:00, i=5: 15:00, i=7: 16:00)。
    assert fired_at == [1, 3, 5, 7]
    assert src.eval_count == 4


def test_unknown_eval_bar_interval_raises_value_error():
    meta = _meta(timeframe="4h")
    src = strategy_adapter.build_intent_source(
        meta, conn=object(), pair="USDJPY", dataset=DATASET_1M,
        resolved=_EMPTY,
        settings=SETTINGS, session=_FakeSession())
    # F5 (sonnet Minor — レビュー fix round 1): エラー文言固有の部分文字列
    # に絞る (本プランのテスト規約)。F1 で幅導出を runner.parse_timeframe
    # へ委譲したため、実際に送出されるのはそちらのメッセージ。
    with pytest.raises(ValueError, match="unsupported eval_timeframe"):
        src(_bar(H, interval="not-a-real-interval"))


# --- ②渡る df: 末尾が評価時点より未来を含まない + len(df) <= max_bars -----

def test_df_passed_to_session_has_no_lookahead_and_respects_max_bars(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    # params は既定の {} と区別できる値にする — advisor 指摘: "params":
    # self._meta.params を "params": {} に劣化させる変異が、既定 {} の
    # meta では検出できず生存し得る。
    meta = _meta(timeframe="1h", max_bars=2, params={"period": 7})
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY, session=session)

    closed_bar = _bar(H + timedelta(hours=5))  # bucket_end = H+6h
    result = src(closed_bar)
    assert result is None  # hold
    assert len(session.calls) == 1
    payload = session.calls[0]
    # [indicator-consumption-wiring] T3: indicators は worker 内で計算
    # されるので payload に載らない — 契約は {"df", "params"} のみ。
    assert set(payload) == {"df", "params"}
    assert payload["params"] == {"period": 7}
    df = payload["df"]
    assert len(df) <= meta.max_bars
    assert len(df) == meta.max_bars  # 十分な履歴があるので上限まで埋まる
    bucket_end = H + timedelta(hours=6)
    width = timedelta(hours=1)
    # 末尾バケットの終端が評価時点 (bucket_end) を超えない (先読み禁止)。
    assert (df.index[-1].to_pydatetime() + width) <= bucket_end
    # 最終バケットは評価対象そのもの ([H+5h, H+6h)) であるべき。
    assert df.index[-1].to_pydatetime() == H + timedelta(hours=5)


# --- F1: `resolved` の必須化 + `decision_sink` + `cpu_sec` (T3 Step 3-1) ----

def test_build_intent_source_requires_resolved(tmp_path):
    """F1: `resolved` 無しは TypeError、worker は 1 つも起動しない。"""
    import subprocess
    conn = _conn(tmp_path)
    meta = _meta(timeframe="1h")
    calls = []
    real = subprocess.Popen
    try:
        subprocess.Popen = lambda *a, **k: calls.append(a)
        with pytest.raises(TypeError):
            strategy_adapter.build_intent_source(
                meta, conn=conn, pair="USDJPY", dataset=DATASET_1M,
                settings=SETTINGS)
    finally:
        subprocess.Popen = real
    assert calls == []


def test_resolved_is_passed_to_the_lazily_created_session(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="1h")
    seen = {}

    class _Spy:
        def __init__(self, meta_arg, *, settings, resolved=None):
            seen["resolved"] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            return dict(_HOLD_RESULT)
        def close(self):
            return None
        cpu_sec = 1.25

    monkeypatch.setattr(strategy_adapter, "PluginSession", _Spy)
    sentinel = ResolvedIndicatorSet.empty(Path("/plugins"))
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=sentinel)
    src(_bar(H))
    assert seen["resolved"] is sentinel      # 同一オブジェクト (再解決しない)
    src.close()
    assert src.cpu_sec == 1.25


def test_decision_sink_records_every_evaluation_including_holds(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="1h")
    open_result = {"action": "open", "rationale": "x", "direction": "long",
                   "entry_type": "market", "limit_price": None,
                   "stop_loss": 99.0, "take_profit": 101.0}
    session = _FakeSession(results=[dict(_HOLD_RESULT), open_result])
    recorded = []
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY, session=session, decision_sink=lambda ts, d:
            recorded.append((ts, d)))
    src(_bar(H))                      # hold
    src(_bar(H + timedelta(hours=1)))  # open
    assert [ts for ts, _ in recorded] == [H + timedelta(hours=1),
                                          H + timedelta(hours=2)]
    assert recorded[0][1]["action"] == "hold"
    assert recorded[1][1]["action"] == "open"
    assert recorded[1][1]["stop_loss"] == 99.0


def test_decision_sink_defaults_to_none_in_production_path(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    src = strategy_adapter.build_intent_source(
        _meta(timeframe="1h"), conn=conn, pair="USDJPY", dataset=DATASET_1M,
        settings=SETTINGS, resolved=_EMPTY, session=_FakeSession())
    assert src._decision_sink is None


def test_no_fire_when_df_is_empty(tmp_path):
    """発火格子に乗っていてもデータが無ければ評価しない (fail closed)。"""
    conn = _conn(tmp_path)  # 履歴を一切 seed しない
    meta = _meta(timeframe="1h")
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)
    result = src(_bar(H))
    assert result is None
    assert session.calls == []


def test_5m_dataset_uses_5m_base_interval_for_load_resampled_frame(tmp_path):
    """段 0 pin: `__call__` が `load_resampled_frame` へ渡す `base_interval`
    が `self._dataset.base_interval` ではなく "1m" 固定へ退行していないか
    を直接検証する。dataset=5m、history には 5m 行のみ (1m 行は 0 件) を
    seed し、"1m" 固定なら df が空になって発火しないところを、正しく
    dataset.base_interval="5m" を使えば発火することをピンする。
    """
    from agentic_fx.backtest.dataset import HistoryDataset

    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=5 * i), o=100.0, h=100.0, l=100.0,
                    c=100.0) for i in range(60)]
    rows = [(r[0], "5m", r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows]
    ohlcv_store.import_history_bars(conn, rows, source="dukascopy")
    meta = _meta(timeframe="1h")
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY",
        resolved=_EMPTY,
        dataset=HistoryDataset("dukascopy", "5m"), settings=SETTINGS,
        session=session)
    src(_bar(H + timedelta(hours=1)))
    assert session.calls, "5m dataset の base_interval が伝播していれば発火するはず"


def test_build_intent_source_uses_dataset_source_not_settings_eval_source(
        tmp_path):
    """段階 2 レビュー是正 C1-5: `build_intent_source` が
    `load_resampled_frame` へ渡す `source` が `dataset.source` ではなく
    `settings.backtest.eval_source` (既定 "dukascopy") へ退行していないか
    を直接検証する。history は `dataset.source="mt5"` でのみ seed し
    (settings.backtest.eval_source の "dukascopy" 側は 0 件)、
    settings.eval_source 固定なら df が空になって発火しないところを、
    正しく dataset.source="mt5" を使えば発火することをピンする。
    """
    assert SETTINGS.backtest.eval_source != "mt5"
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100.0, h=100.0, l=100.0,
                    c=100.0) for i in range(60)]
    ohlcv_store.import_history_bars(conn, rows, source="mt5")
    meta = _meta(timeframe="1h")
    session = _FakeSession()
    from agentic_fx.backtest.dataset import HistoryDataset
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY",
        resolved=_EMPTY,
        dataset=HistoryDataset("mt5", "1m"), settings=SETTINGS,
        session=session)
    src(_bar(H + timedelta(hours=1)))
    assert session.calls, "dataset.source が伝播していれば発火するはず"


def test_meta_max_bars_is_passed_to_load_resampled_frame(tmp_path, monkeypatch):
    """codex r1 束2 Minor (= 段 0 束 3 提案 2): `max_bars=None` への変異は
    既存 E2E を通過してしまう (履歴が上限より短ければ結果が変わらない)。
    strategy の宣言値がそのまま `load_resampled_frame` の `max_bars` kwarg
    に届くことを spy で直接 pin する。`strategy_adapter` は
    `from ... import load_resampled_frame` で取り込んでいるので、差し替える
    のは **call site の名前** (`strategy_adapter.load_resampled_frame`)。
    """
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    seen: list[dict] = []
    real = strategy_adapter.load_resampled_frame

    def spy(*args, **kwargs):
        seen.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(strategy_adapter, "load_resampled_frame", spy)
    meta = _meta(timeframe="1h", max_bars=37)      # 既定 200 と区別できる値
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", resolved=_EMPTY,
        dataset=DATASET_1M, settings=SETTINGS, session=session)
    src(_bar(H + timedelta(hours=1)))
    assert seen, "load_resampled_frame が呼ばれていない"
    assert "max_bars" in seen[0], "max_bars 引数そのものが落ちている"
    assert seen[0]["max_bars"] == 37, (
        "meta.max_bars が伝播していない (None 固定・既定値固定への退行)")


# --- ③ open → intent dict 写像 ---------------------------------------------

def test_open_market_without_take_profit_omits_take_profit_key(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    meta = _meta(timeframe="1h")
    session = _FakeSession(results=[{
        "action": "open", "rationale": "good setup", "direction": "long",
        "entry_type": "market", "limit_price": None, "stop_loss": 149.0,
        "take_profit": None}])
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)

    intent = src(_bar(H))
    assert intent == {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "market", "horizon": "day", "stop_loss": 149.0,
        "confidence": 0.5, "reasoning": "good setup"}
    assert "take_profit" not in intent
    assert "limit_price" not in intent
    assert "expires_in" not in intent


def test_open_market_with_take_profit_includes_take_profit_key(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    meta = _meta(timeframe="1h")
    session = _FakeSession(results=[{
        "action": "open", "rationale": "tp set", "direction": "short",
        "entry_type": "market", "limit_price": None, "stop_loss": 151.0,
        "take_profit": 148.0}])
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)

    intent = src(_bar(H))
    assert intent["take_profit"] == 148.0
    assert "limit_price" not in intent
    assert "expires_in" not in intent


def test_open_limit_includes_limit_price_and_expires_in(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    meta = _meta(timeframe="1h")
    session = _FakeSession(results=[{
        "action": "open", "rationale": "breakout", "direction": "long",
        "entry_type": "limit", "limit_price": 150.5, "stop_loss": 149.5,
        "take_profit": 152.0}])
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)

    intent = src(_bar(H))
    assert intent["entry_type"] == "limit"
    assert intent["limit_price"] == 150.5
    assert intent["expires_in"] == "4h"
    assert intent["take_profit"] == 152.0


def test_hold_maps_to_none(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    meta = _meta(timeframe="1h")
    session = _FakeSession(results=[dict(_HOLD_RESULT)])
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)
    assert src(_bar(H)) is None


# --- ④ SandboxError 貫通 -----------------------------------------------------

def test_sandbox_error_propagates_uncaught(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    meta = _meta(timeframe="1h")
    session = _FakeSession(raises=SandboxError("plugin crashed"))
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=session)
    with pytest.raises(SandboxError, match="plugin crashed"):
        src(_bar(H))


# --- セッション所有権 (session=None は lazy 生成・自前 close。注入は所有しない) --

class _RecordingSession:
    """`strategy_adapter.PluginSession` を monkeypatch で差し替えるための
    fake クラス (実サブプロセスを起動しない)。`created` リストへ自身を
    登録するので、いつ生成されたかをテストから観測できる。"""

    def __init__(self, meta, *, settings) -> None:
        self.meta = meta
        self.settings = settings
        self.entered = False
        self.closed = False
        self.calls: list[dict] = []

    def __enter__(self) -> "_RecordingSession":
        self.entered = True
        return self

    def call(self, payload: dict) -> dict:
        self.calls.append(payload)
        return dict(_HOLD_RESULT)

    def close(self) -> None:
        self.closed = True


def test_session_none_creates_lazily_on_first_fire_only(tmp_path, monkeypatch):
    created: list[_RecordingSession] = []

    def _factory(meta, *, settings, resolved=None):
        session = _RecordingSession(meta, settings=settings)
        created.append(session)
        return session

    monkeypatch.setattr(strategy_adapter, "PluginSession", _factory)

    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="4h")
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY)

    assert created == []  # 構築時点ではまだ何も起動しない

    src(_bar(H))  # i=0, 発火格子外 (4h 境界に乗らない) — セッション未生成のまま
    assert created == []

    src(_bar(H + timedelta(hours=3)))  # i=3, 発火 → ここで初めて lazy 生成
    assert len(created) == 1
    assert created[0].entered
    assert len(created[0].calls) == 1

    src(_bar(H + timedelta(hours=7)))  # i=7, 再度発火 — 同じセッションを使い回す
    assert len(created) == 1
    assert len(created[0].calls) == 2

    src.close()
    assert created[0].closed


def test_close_does_not_close_injected_session(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 61)
    meta = _meta(timeframe="1h")
    fake = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY,
        session=fake)
    src(_bar(H))
    src.close()
    assert fake.closed is False  # 所有権は注入者にある — アダプタは閉じない


# --- ⑤ 統合: 1h 宣言 plugin + 5 時間 replay -----------------------------------

# sma_cross サンプル (docs/examples/plugins) はプラン 7 Task 6 の修正で
# take_profit を返すようになったが、本テストは fast_period=1/slow_period=2
# のような極端なパラメータで数本のうちに確実にクロスを起こしたい (5 時間
# replay という短い窓でも発火させるため)。共有サンプルのパラメータを
# テスト都合で変えたくないので、引き続きこのテスト専用の最小 SMA クロス
# plugin (take_profit も返す) を用意する。
_CROSS_WITH_TP_PY = """
import pandas as pd


def evaluate(df, indicators, signals, params):
    fast_period = int(params.get("fast_period", 1))
    slow_period = int(params.get("slow_period", 2))
    sl_pips = float(params.get("stop_loss_pips", 20))
    tp_pips = float(params.get("take_profit_pips", 60))
    pip_size = float(params.get("pip_size", 0.01))

    if len(df) < slow_period + 1:
        return {"action": "hold", "rationale": "insufficient bars for warmup"}

    fast = df["close"].rolling(window=fast_period).mean()
    slow = df["close"].rolling(window=slow_period).mean()
    prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
    curr_fast, curr_slow = fast.iloc[-1], slow.iloc[-1]
    if (pd.isna(prev_fast) or pd.isna(prev_slow)
            or pd.isna(curr_fast) or pd.isna(curr_slow)):
        return {"action": "hold", "rationale": "insufficient bars for warmup"}

    prev_diff = prev_fast - prev_slow
    curr_diff = curr_fast - curr_slow
    last_close = float(df["close"].iloc[-1])
    sl_offset = sl_pips * pip_size
    tp_offset = tp_pips * pip_size

    if prev_diff <= 0 and curr_diff > 0:
        return {"action": "open", "direction": "long", "entry_type": "market",
                "stop_loss": last_close - sl_offset,
                "take_profit": last_close + tp_offset, "rationale": "cross up"}
    if prev_diff >= 0 and curr_diff < 0:
        return {"action": "open", "direction": "short", "entry_type": "market",
                "stop_loss": last_close + sl_offset,
                "take_profit": last_close - tp_offset, "rationale": "cross down"}
    return {"action": "hold", "rationale": "no crossover"}
"""


def _write_cross_with_tp_plugin(plugin_dir: Path) -> None:
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_CROSS_WITH_TP_PY)
    (plugin_dir / "config.yaml").write_text(
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
        "exit_mode: levels\nmax_bars: 200\n")


def test_integration_plugin_5h_replay_reaches_orders_with_single_session(
        tmp_path, monkeypatch):
    """実 `PluginSession` (実サブプロセス) を使い、5 時間 replay で
    strategy plugin の open intent が実際に `orders` テーブルへ到達する
    ことと、バックテスト全体でサンドボックスプロセスが 1 個だけ起動された
    こと (`subprocess.Popen` 呼び出し回数・pid) を確認する。"""
    from agentic_fx.plugin import sandbox as sandbox_module

    captured_procs: list = []
    real_popen = sandbox_module.subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured_procs.append(proc)
        return proc

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", _capturing_popen)

    hist_conn = _conn(tmp_path)
    # hour0=100, hour1=99, hour2=110 (fast(1)/slow(2) クロス発生) — 以降 flat。
    targets = [100.0, 99.0, 110.0, 110.0, 110.0]
    rows = []
    for hour, price in enumerate(targets):
        rows.extend(
            _row_at(H + timedelta(hours=hour, minutes=m), o=price, h=price,
                   l=price, c=price)
            for m in range(60))
    ohlcv_store.import_history_bars(hist_conn, rows, source="dukascopy")

    plugin_dir = tmp_path / "plugin_src" / "cross_with_tp"
    _write_cross_with_tp_plugin(plugin_dir)
    meta = PluginMeta(
        name="cross_with_tp", kind="strategy", path=plugin_dir,
        params={"fast_period": 1, "slow_period": 2, "stop_loss_pips": 20,
               "take_profit_pips": 60, "pip_size": 0.01},
        timeframe="1h", pairs=("USDJPY",), max_bars=200,
        content_hash=_real_content_hash(plugin_dir))

    intent_source = strategy_adapter.build_intent_source(
        meta, conn=hist_conn, pair="USDJPY", dataset=DATASET_1M,
        resolved=_EMPTY,
        settings=SETTINGS)
    session_pid = None
    try:
        result = run_replay(
            SETTINGS, symbol="USDJPY", dataset=DATASET_1M, start=H,
            end=H + timedelta(hours=5), intent_source=intent_source,
            eval_timeframe="1h", history_conn=hist_conn)
        # close() 前に pid を記録する (close() は所有セッションを None に
        # 落とす — セッション 1 プロセス確認は close 前でなければ見えない)。
        session_pid = intent_source._session.pid
    finally:
        intent_source.close()

    assert result.orders  # intent が executor まで届き orders 行になった
    assert intent_source.eval_count == 4  # [H,17:00) 内の 1h 境界 4 回
    assert len(captured_procs) == 1  # サブプロセス起動は 1 回のみ (セッション使い回し)
    assert session_pid is not None
    assert session_pid == captured_procs[0].pid


# ---- Task 3: producer 対称化 ---------------------------------------------------

def test_build_intent_source_rejects_pair_not_in_meta_pairs(tmp_path):
    """producer 側 (settings.pairs 外は warning+skip) と対称の検証:
    adapter は 1 インスタンス = 1 pair の明示的構築のため、meta.pairs に
    無い pair を渡されたら即座に ValueError (fail closed, Fable M-1)。"""
    meta = _meta(pairs=("USDJPY",))
    with pytest.raises(ValueError, match="pairs"):
        strategy_adapter.build_intent_source(
            meta, conn=_conn(tmp_path), pair="EURUSD",
            resolved=_EMPTY,
            dataset=DATASET_1M, settings=SETTINGS)


def test_plugin_strategy_intent_source_no_longer_accepts_source_kwarg():
    """I1 (codex 段階2/3 是正 1周目): `PluginStrategyIntentSource` は
    `source` と `dataset` の両方を受けていたため、直接構築で
    `source="dukascopy"` + `dataset=HistoryDataset("mt5", "5m")` のような
    split-brain (SQL は source=self._source, base_interval=
    self._dataset.base_interval で混成) が型で成立してしまっていた。
    `source` 引数を廃止し dataset のみを受けること (TypeError で拒否)。
    """
    with pytest.raises(TypeError):
        strategy_adapter.PluginStrategyIntentSource(
            _meta(), conn=None, pair="USDJPY", source="dukascopy",
            dataset=DATASET_1M, settings=SETTINGS)


# --- AST 全数性検査 (codex plan r1 I2、T1 Step 1-7 と同じ流儀) --------------

def test_no_caller_calls_build_intent_source_without_resolved():
    """[indicator-consumption-wiring] T3 (F1): `build_intent_source(...)` の
    呼び出しが `src` にも `tests` にも `resolved=` 無しで残っていないこと。
    `resolved` はキーワード必須なので実行すれば `TypeError` になるが、
    patch された経路・未到達分岐では発見が遅れる。**`ast.Call` だけを見る**
    (docstring 言及箇所を拾わないため)。`**kwargs` で受け流す fake の
    定義側は `ast.Call` ではないので対象外。"""
    import ast
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    offenders = []
    for root in (repo / "src" / "agentic_fx", repo / "tests"):
        for path in sorted(root.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = (f.attr if isinstance(f, ast.Attribute)
                        else f.id if isinstance(f, ast.Name) else None)
                if name != "build_intent_source":
                    continue
                if not any(kw.arg == "resolved" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(repo)}:{node.lineno}")
    assert offenders == [], (
        "build_intent_source(...) を resolved= 無しで呼んでいる箇所: "
        f"{offenders}")

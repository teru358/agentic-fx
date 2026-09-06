"""BacktestRunner — in-memory 再生・synthetic Mission・先読み禁止 (Task 7)。

上書き節 (task-7-brief.md 末尾, コントローラ照合 2026-08-01) がこのテストの
契約:
- bars_fn/quote_fn は latest_completed_1m を使う (bar_at は先読みになる)。
- バケット集約だけは feed.bar_at で直接読む (集約時点で全て完成済み)。
- フィクスチャタイムラインは 13:00/13:01/13:03/13:04 (完成足配線に伴う 1
  tick 後ろへのずれ)。
"""
from datetime import datetime, timedelta, timezone

import pytest

import agentic_fx.backtest.runner as runner_module
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import missions, orders, ohlcv, snapshots
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore
from agentic_fx.activity import ActivityLog

from agentic_fx.backtest.replay import BarFeed
from agentic_fx.backtest.runner import _aggregate_bucket, run_replay
from agentic_fx.backtest.dataset import HistoryDataset

from tests.backtest.factories import H, WED, SETTINGS, _conn, _row_at, DATASET_1M


OPEN = {"action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day",
        "limit_price": 148.20, "expires_in": "6h",
        "stop_loss": 147.80, "take_profit": 149.00,
        "reasoning": "bt"}

OPEN_MARKET = {"action": "open", "pair": "USDJPY", "direction": "long",
              "entry_type": "market", "horizon": "day",
              "stop_loss": 148.30, "take_profit": 149.00,
              "reasoning": "f4"}


def _seed_history(conn):
    """水曜 12:00 から: 1h バー確定 → (次 tick 後に) 指値到達 → TP 到達の 1m 列。

    タイムライン (完成足配線 — 上書き節 A/B。行データは brief のまま):
      13:00 tick = bucket [12:00,13:00) 確定 → proposal 生成 (集約は feed 直読み)
      13:01 tick = tick() 後に pending 執行 → 指値 148.20 発注
                   (quote は完成バー 13:00、close 148.35)
      13:02 tick = 指値まだ到達しない (完成バーは 13:01)
      13:03 tick = fills が完成バー 13:02 (low 148.10) で指値到達を判定
      13:04 tick = 完成バー 13:03 (high 149.10) で TP 到達
    """
    rows = []
    t = WED  # 2026-07-22 (水) 12:00 UTC — 市場オープン
    for i in range(60):          # 12:00-12:59 (評価対象の 1h を構成)
        rows.append(_row_at(t + timedelta(minutes=i), o=148.5, h=148.6,
                            l=148.4, c=148.5))
    for m in (60, 61):           # 13:00, 13:01 — 指値に届かないバー
        rows.append(_row_at(t + timedelta(minutes=m), o=148.4, h=148.45,
                            l=148.30, c=148.35))
    rows.append(_row_at(t + timedelta(minutes=62), o=148.3, h=148.35,
                        l=148.10, c=148.15))       # 13:02 — 指値 148.20 到達
    rows.append(_row_at(t + timedelta(minutes=63), o=148.9,
                        h=149.10, l=148.85, c=149.05))  # 13:03 — TP 到達
    ohlcv.import_history_bars(conn, rows, source="dukascopy")


def test_full_cycle_open_fill_tp(tmp_path):
    hist = _conn(tmp_path)
    _seed_history(hist)
    fired = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN)
        return None

    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=source, eval_timeframe="1h",
                     history_conn=hist)
    closed = [o for o in res.orders if o["status"] == "closed"]
    assert len(closed) == 1 and closed[0]["realized_pnl"] > 0
    assert closed[0]["close_reason"] == "tp"
    assert fired == [WED]  # 評価は 12:00-12:59 の 1h バー確定時のみ
    # 上書き節 B のタイムライン (完成足配線) を厳密にピンする — advisor
    # 指摘: bars_fn/quote_fn に latest_completed_1m の代わりに bar_at を
    # 誤配線しても status/realized_pnl/close_reason は変わらず、この
    # アサーションが無いと先読みが混入したまま緑になる。
    # fills が 13:03 tick (完成バー 13:02, low 148.10) で指値到達判定、
    # TP は 13:04 tick (完成バー 13:03, high 149.10) で確定する。
    assert closed[0]["filled_at"].startswith("2026-07-22T13:03")
    assert closed[0]["closed_at"].startswith("2026-07-22T13:04")


def test_limit_price_only_reachable_within_eval_bucket_never_fills(tmp_path):
    """評価バケット内 (12:00-12:59) にしか指値到達価格が無く、それ以降に
    データが 1 本も無い場合は約定しない。

    F6 (sonnet M2 — 名称訂正): 旧名 `test_no_lookahead_same_bar` は brief が
    「この契約 (先読み禁止) をピンする」と名指ししていたが、13:01 以降の
    データが無いため、bars_fn が `latest_completed_1m` (正) を使っても
    `bar_at` (先読みバグ) を使っても結果が変わらない vacuous なテストだった
    (実装者自身の変異記録: mutation 1 で SURVIVED と確認済み)。先読み禁止
    そのもののピンは `test_limit_fill_uses_only_completed_bar_not_forming_bar`
    に担わせ、このテストは実態どおり「フィード欠損時に指値が誤って約定
    しない」ことの回帰テストとして名前を訂正して残す。
    """
    hist = _conn(tmp_path)
    # 12:00-12:59 の 1h バー自体に指値到達価格を含める (13:00 以降は到達しない)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.10, c=148.5) for i in range(60)]
    rows.append(_row_at(WED + timedelta(hours=1), o=148.5, h=148.6,
                        l=148.4, c=148.5))
    ohlcv.import_history_bars(conn=hist, rows=rows, source="dukascopy")
    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert not [o for o in res.orders if o["status"] in ("open", "closed")]


def test_limit_fill_uses_only_completed_bar_not_forming_bar(tmp_path):
    """先読み禁止 (§6) の核心 — 指値到達判定は「完成済み」1m バーのみを見る。

    F6 (sonnet M2 の是正): `bars_fn` が正しく `latest_completed_1m` を使う
    限り、tick=13:02 の時点でまだ「形成中」の 13:02 バー (低値 148.10、
    指値到達) は見えず、13:01 の完成バー (届かない) しか見えない。指値
    到達が観測できるのは、13:02 バーが完成し終える tick=13:03 になって
    初めてである。`bars_fn` を誤って `bar_at` (形成中バーを直読み) に配線
    すると、tick=13:02 の時点で既に (本来まだ見えないはずの) 13:02 バーの
    低値が見え、1 tick 早く約定してしまう — `filled_at` の値でこれを
    ピンする。
    """
    hist = _conn(tmp_path)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6, l=148.4,
                    c=148.5) for i in range(60)]              # 12:00-12:59 評価用 (届かない)
    rows.append(_row_at(WED + timedelta(hours=1), o=148.5, h=148.6,
                        l=148.4, c=148.5))                     # 13:00 entry quote (届かない)
    rows.append(_row_at(WED + timedelta(hours=1, minutes=1), o=148.5,
                        h=148.6, l=148.4, c=148.5))             # 13:01 完成バー (届かない)
    rows.append(_row_at(WED + timedelta(hours=1, minutes=2), o=148.3,
                        h=148.35, l=148.10, c=148.15))          # 13:02 完成バー (指値到達)
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    filled = [o for o in res.orders if o["status"] in ("open", "closed")]
    assert len(filled) == 1
    # latest_completed_1m を正しく使えば、13:02 バー (低値 148.10) は
    # tick=13:03 の判定で初めて「完成済み」として見える — bar_at (形成中
    # バー) を誤って使うと 1 tick 早い 13:02 で約定してしまう。
    assert filled[0]["filled_at"].startswith("2026-07-22T13:03")


def test_synthetic_mission_passes_origin_gate(tmp_path):
    """§5 検証を同一コードで通す — missions 行が in-memory に作られ intent が accepted。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert res.orders  # origin_rejected なら orders は生まれない


def test_real_db_untouched(tmp_path, monkeypatch):
    """バックテストが実 data/ に触れない。"""
    monkeypatch.chdir(tmp_path)
    hist = _conn(tmp_path)
    _seed_history(hist)
    run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
               start=WED, end=WED + timedelta(hours=1),
               intent_source=lambda b: None, eval_timeframe="1h",
               history_conn=hist)
    assert not (tmp_path / "data").exists()


def test_initial_balance_wiring_no_spurious_killswitch(tmp_path):
    """backtest.initial_balance ≠ paper.starting_balance でも初回 tick で
    kill switch がラッチしない (残高の二重基準を塞ぐ)。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    s = SETTINGS.model_copy(update={"backtest": SETTINGS.backtest.model_copy(
        update={"initial_balance": 5_000_000.0})})   # paper 側は 1,000,000 のまま
    res = run_replay(s, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    closed = [o for o in res.orders if o["status"] == "closed"]
    assert closed  # ラッチしていれば gate_rejected で 0 件になる
    assert res.equity_curve[0][1] == 5_000_000.0


def test_fallback_spread_used_when_db_spread_missing(tmp_path):
    """DB の spread 列が欠損 (None) の場合、assumed_spread_pips ベースの
    フォールバックへ切り替わり fallback_spread_used=True になる。

    advisor 指摘: `_seed_history`/`_row_at` は既定で spread=0.01 を書き込む
    ため、通常のテストではフォールバック経路 (quote_from_bar を呼ぶ**前**
    に spread を解決する分岐) が一度も実行されず変異に対して無防備だった。
    ここでは全行 spread=None で書き込み、フォールバック分岐 (quote_fn の
    None spread ハンドリング) を強制的に通す。フォールバック解決が
    quote_from_bar 呼び出しの**後**に退化する変異が起きると、Task 6 の
    契約 (spread=None は ValueError) により quote_fn 自体が例外を投げ、
    以下 orders が 1 件も作られなくなる — それも検出できるよう orders が
    実際に作られていることも併せて assert する。
    """
    hist = _conn(tmp_path)
    rows = []
    t = WED
    for i in range(60):
        rows.append(_row_at(t + timedelta(minutes=i), o=148.5, h=148.6,
                            l=148.4, c=148.5, spread=None))
    for m in (60, 61):
        rows.append(_row_at(t + timedelta(minutes=m), o=148.4, h=148.45,
                            l=148.30, c=148.35, spread=None))
    rows.append(_row_at(t + timedelta(minutes=62), o=148.3, h=148.35,
                        l=148.10, c=148.15, spread=None))
    rows.append(_row_at(t + timedelta(minutes=63), o=148.9, h=149.10,
                        l=148.85, c=149.05, spread=None))
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert res.fallback_spread_used is True
    assert res.orders  # フォールバックが正しく機能していれば発注は成立する


def test_bucket_alignment_uses_utc_epoch_anchor(tmp_path):
    """バケット境界の錨は UTC epoch (実運用 datafeed.bars.BAR_ANCHOR="epoch"
    と同じ規律) — start がその格子に乗っていない再生でも、実運用と同じ
    [13:00,14:00) 等の境界で評価する。start 相対 (elapsed % tf) に退化
    すると [13:30,14:30) のような実運用に存在しない境界で評価してしまう。
    """
    off_grid_start = WED + timedelta(minutes=30)  # 12:30 — 1h グリッドから外れる
    hist = _conn(tmp_path)
    rows = [_row_at(off_grid_start + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.4, c=148.5)
           for i in range(180)]  # 12:30-15:29 の連続 1m
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    fired = []
    run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
              start=off_grid_start, end=off_grid_start + timedelta(hours=3),
              intent_source=lambda b: fired.append(b.ts),
              eval_timeframe="1h", history_conn=hist)
    assert fired  # 少なくとも 1 回は評価が起きる
    assert all(ts.minute == 0 and ts.second == 0 for ts in fired), fired


# ---------------------------------------------------------------------------
# fix round 1 (コントローラ 2026-08-01 — sonnet + codex 並行レビュー統合裁定)
# ---------------------------------------------------------------------------

def test_aggregate_bucket_ohlcv_values(tmp_path):
    """F5 (sonnet I3 — `_aggregate_bucket` の OHLCV 集約値そのものが
    無検証だった。`high` を先頭バーの値に差し替える変異が SURVIVED)。

    この Bar はプラン 7 の strategy plugin が受け取る唯一の入力契約であり、
    open=先頭バーの open / high=全体最大 / low=全体最小 / close=末尾バーの
    close / volume=合計、を直接ピンする。
    """
    hist = _conn(tmp_path)
    rows = [
        _row_at(WED, o=100.0, h=101.0, l=99.0, c=100.5),
        _row_at(WED + timedelta(minutes=1), o=100.5, h=105.0, l=100.0, c=102.0),
        _row_at(WED + timedelta(minutes=2), o=102.0, h=103.0, l=95.0, c=101.0),
    ]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    feed = BarFeed(hist, "USDJPY", dataset=DATASET_1M, start=WED,
                   end=WED + timedelta(minutes=3))
    bar = _aggregate_bucket(feed, "USDJPY", "3m", WED, timedelta(minutes=3))
    assert bar is not None
    assert bar.open == 100.0     # 先頭バーの open
    assert bar.high == 105.0     # 全体最大 (2 本目)
    assert bar.low == 95.0       # 全体最小 (3 本目)
    assert bar.close == 101.0    # 末尾バーの close
    assert bar.volume == 30.0    # 合計 (10 + 10 + 10)


def test_bucket_evaluation_skipped_when_market_closed(tmp_path):
    """F2/I2 (codex Important + sonnet Important — sonnet 変異①の再現)。

    brief 本文 §6「is_market_open(now) が偽の間は評価しない (週末に評価
    だけ走る歪みを防ぐ)」— 金曜クローズ後 (NY 18:00, 夏時間で 22:00 UTC)
    から土曜にかけて、バケットは確定してもクローズ中は intent_source を
    一切呼んではならない。
    """
    fri_close = datetime(2026, 7, 24, 22, 0, tzinfo=timezone.utc)  # 金 22:00 UTC (NY 18:00, クローズ後)
    hist = _conn(tmp_path)
    rows = [_row_at(fri_close + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.4, c=148.5)
           for i in range(180)]  # 3h 分の連続 1m (バケット確定用データ)
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    fired = []
    run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
              start=fri_close, end=fri_close + timedelta(hours=3),
              intent_source=lambda b: fired.append(b.ts),
              eval_timeframe="1h", history_conn=hist)
    assert fired == []  # 週末クローズ中はバケットが確定しても評価されない


def test_pending_proposal_discarded_when_market_closes_before_execution(tmp_path):
    """F2 (codex Important + sonnet Important)。

    評価時 (bucket 確定 tick) は市場オープンでも、1 tick 遅れの執行時に
    市場がクローズしていれば発注してはならない。金曜 20:59 UTC (夏時間、
    NY 16:59 でオープン中) で提案生成 → 21:00 UTC (NY 17:00, クローズ瞬間)
    の執行 tick では発注が起きず orders は空のまま。
    """
    fri_start = datetime(2026, 7, 24, 20, 58, tzinfo=timezone.utc)
    hist = _conn(tmp_path)
    rows = [_row_at(fri_start + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.4, c=148.5) for i in range(6)]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    fired = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN)
        return None

    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=fri_start, end=fri_start + timedelta(minutes=5),
                     intent_source=source, eval_timeframe="1m",
                     history_conn=hist)
    assert fired  # 評価自体はオープン中 (20:59) に起きている
    assert not res.orders  # クローズ後の執行は破棄される


def test_pending_proposal_is_dropped_and_recorded_when_execution_bar_missing(
        tmp_path, monkeypatch):
    """M6/M7: 次tickだけ欠損しても完走し、提案破棄をactivityへ残す。"""
    hist = _conn(tmp_path)
    ohlcv.import_history_bars(
        hist, [_row_at(WED, o=148.5, h=148.6, l=148.4, c=148.5)],
        source="dukascopy")
    recorded = []
    real_write = runner_module._RecordingActivity.write

    def capture(self, category, event, summary, ref_id=None):
        real_write(self, category, event, summary, ref_id)
        recorded.extend(self.entries[-1:])

    monkeypatch.setattr(runner_module._RecordingActivity, "write", capture)
    res = run_replay(
        SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
        start=WED, end=WED + timedelta(minutes=3),
        intent_source=lambda b: dict(OPEN_MARKET), eval_timeframe="1m",
        history_conn=hist)

    assert res.orders == []
    dropped = [entry for entry in recorded
               if entry["kind"] == "proposal_dropped_no_bar"]
    assert len(dropped) == 1
    assert (WED + timedelta(minutes=2)).isoformat() in dropped[0]["text"]


def test_pending_proposal_opens_when_execution_bar_exists(tmp_path):
    """fail-soft追加後も足がある通常の執行経路は維持する。"""
    hist = _conn(tmp_path)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.4, c=148.5) for i in range(2)]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    res = run_replay(
        SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
        start=WED, end=WED + timedelta(minutes=3),
        intent_source=lambda b: dict(OPEN_MARKET), eval_timeframe="1m",
        history_conn=hist)
    assert len(res.orders) == 1
    assert res.orders[0]["status"] == "open"


def test_quote_error_names_5m_base_interval(tmp_path, monkeypatch):
    """M8: quoteの最後の網はdatasetの5m粒度を正確に報告する。"""
    hist = _conn(tmp_path)
    rows = [
        ("USDJPY", "5m", (WED + timedelta(minutes=i)).isoformat(),
         148.5, 148.6, 148.4, 148.5, 10.0, 0.01)
        for i in (0, 5)
    ]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    def remove_guarded_bar_then_quote(self, intent, mission_id):
        feed = next(cell.cell_contents for cell in self.quote_fn.__closure__
                    if isinstance(cell.cell_contents, BarFeed))
        feed._bars.pop((WED + timedelta(minutes=5)).isoformat())
        return self.quote_fn(intent.pair)

    monkeypatch.setattr(Executor, "handle_intent", remove_guarded_bar_then_quote)
    with pytest.raises(ValueError, match="no completed 5m bar"):
        run_replay(
            SETTINGS, symbol="USDJPY", dataset=HistoryDataset("dukascopy", "5m"),
            start=WED, end=WED + timedelta(minutes=15),
            intent_source=lambda b: dict(OPEN_MARKET), eval_timeframe="5m",
            history_conn=hist)


def test_pending_execution_happens_after_tick_not_before(tmp_path):
    """F4 (sonnet I1 — sonnet 変異②の再現)。

    brief 本文 §6 が「先読み禁止の本体」と名指しする実行順序
    (scheduler.tick → pending 執行) をピンする。順序が入れ替わると、
    市場注文が発注された**同一 tick**で SL/TP 監視 (`_process_exits`) の
    対象になってしまい、正しい順序では次 tick 以降でしか評価されないはず
    の建玉が同じ tick 内で閉じてしまう。

    フィクスチャ: 13:01 tick で成行 (entry_type=market) が発注される。
    entry quote の元になる完成バー (13:00) と、正しい順序ならまだ発注前で
    評価対象にならないその同じバーの high (149.10) が TP (149.00) を
    超える。正しい順序なら 13:01 では未発注 (tick が先) → 発注後に監視
    対象になるのは次 tick (13:02) 以降で、そこでは 13:01 完成バー
    (high=149.10, 同じく TP 到達) を使って初めて閉じる。つまり
    `filled_at` (発注 tick) と `closed_at` (TP 確定 tick) は**同一になり
    得ない**。順序が入れ替わると、発注直後の同一 tick (13:01) の
    `_process_exits` が 13:00 完成バーで即座に TP 判定してしまい、
    `filled_at == closed_at` になる。
    """
    hist = _conn(tmp_path)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6, l=148.4,
                    c=148.5) for i in range(60)]                # 12:00-12:59 評価用
    rows.append(_row_at(WED + timedelta(hours=1), o=148.5, h=149.10,
                        l=148.4, c=148.5))                       # 13:00 — entry quote + TP到達値
    rows.append(_row_at(WED + timedelta(hours=1, minutes=1), o=148.5,
                        h=149.10, l=148.4, c=148.5))              # 13:01 — 正しい順序での TP 判定対象
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    fired = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN_MARKET)
        return None

    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=source, eval_timeframe="1h",
                     history_conn=hist)
    closed = [o for o in res.orders if o["status"] == "closed"]
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "tp"
    # 順序退行 (pending 執行 → tick) だと同一 tick (13:01) で閉じてしまう。
    assert closed[0]["filled_at"] != closed[0]["closed_at"]


def test_end_must_be_on_minute_grid(tmp_path):
    """F3 (codex Important) — `end` も `ReplayClock`/`BarFeed` と同じ正時
    格子 (second==microsecond==0) を要求する。格子外の `end` は宣言した
    `[start, end)` を最大 1 分近く超過して処理してしまう。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    with pytest.raises(ValueError, match="minute boundary"):
        run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                  start=WED, end=WED + timedelta(hours=1, seconds=30),
                  intent_source=lambda b: None, eval_timeframe="1h",
                  history_conn=hist)


def test_parse_timeframe_rejects_zero(tmp_path):
    """F7 (Minor 一括) — "0m"/"0h" は正規表現には通るが
    `tf=timedelta(0)` になり、バケット判定の `% tf` が
    ZeroDivisionError になる。事前に ValueError で拒否する。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    with pytest.raises(ValueError, match="eval_timeframe"):
        run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                  start=WED, end=WED + timedelta(hours=1),
                  intent_source=lambda b: None, eval_timeframe="0m",
                  history_conn=hist)


def test_eval_timeframe_must_be_multiple_of_5m_base_interval(tmp_path):
    """C3-1: parse 可能な 7m でも 5m 基底の整数倍でなければ拒否する。"""
    hist = _conn(tmp_path)
    with pytest.raises(ValueError, match="eval_timeframe must be an integer multiple"):
        run_replay(
            SETTINGS, symbol="USDJPY", dataset=HistoryDataset("dukascopy", "5m"),
            start=WED, end=WED + timedelta(hours=1),
            intent_source=lambda b: None, eval_timeframe="7m",
            history_conn=hist)


def test_eval_timeframe_must_not_be_narrower_than_5m_base_interval(tmp_path):
    """C3-1: 5m 基底より狭い 1m 評価時間足を拒否する。"""
    hist = _conn(tmp_path)
    with pytest.raises(ValueError, match="eval_timeframe must be an integer multiple"):
        run_replay(
            SETTINGS, symbol="USDJPY", dataset=HistoryDataset("dukascopy", "5m"),
            start=WED, end=WED + timedelta(hours=1),
            intent_source=lambda b: None, eval_timeframe="1m",
            history_conn=hist)


def test_equity_curve_has_no_duplicate_timestamps(tmp_path):
    """F7 (sonnet M1) — 先頭の seed 点 (start, initial_balance) と初回
    ループの tail 追記が同一 ts になり二重記録されていた。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(minutes=5),
                     intent_source=lambda b: None, eval_timeframe="1h",
                     history_conn=hist)
    ts_list = [ts for ts, _ in res.equity_curve]
    assert len(ts_list) == len(set(ts_list))
    assert res.equity_curve[0] == (WED.isoformat(), SETTINGS.backtest.initial_balance)


def test_replay_exposes_timestamp_then_id_ordered_snapshots_and_first_decision(
        tmp_path, monkeypatch):
    """観測面を外す変異（snapshots を返さない／同時刻行を id 順にしない）を検出する。"""
    hist = _conn(tmp_path)
    _seed_history(hist)

    real_init_db = runner_module.init_db

    def _init_with_reverse_ties(conn):
        real_init_db(conn)
        conn.execute("DROP INDEX ix_account_snapshots_ts_id")
        conn.execute(
            "CREATE INDEX snapshot_reverse_ties ON account_snapshots(ts, id DESC)")

    # 同一 ts の順序は id を明示しなければ DB 契約に含まれない。逆順 index
    # を使う実 DB で、その不完全な ORDER BY を可観測な誤順序にする。
    monkeypatch.setattr(runner_module, "init_db", _init_with_reverse_ties)

    res = runner_module.run_replay(
        SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
        start=WED, end=WED + timedelta(minutes=2), intent_source=lambda b: None,
        eval_timeframe="1h", history_conn=hist)

    # 段階 3 の先頭足規則 (A2): 最初の意思決定 = ceil_to_bucket(start, eval_tf)
    # + eval_tf。WED は 1h 格子上のため ceil(WED)==WED、よって WED+1h。
    assert res.first_decision_at == WED + timedelta(hours=1)
    assert res.snapshots
    assert [(row["ts"], row["id"]) for row in res.snapshots] == sorted(
        (row["ts"], row["id"]) for row in res.snapshots)


def test_drawdown_kill_switch_latches_and_blocks_next_open(tmp_path):
    """F1(b) (codex Critical の裁定 — equity の二重の意味を実証する対となる
    テスト)。既存の `test_initial_balance_wiring_no_spurious_killswitch` は
    偽陽性 (誤ラッチしない) 側しか検証しておらず、真にドローダウン閾値
    (drawdown_kill_pct=2.0%) を超えた場合に実際にラッチし新規発注を止める
    方向は無検証だった (sonnet ⚠️ Cannot verify)。

    1 件目の成行を建てた直後に巨大なギャップ (SL を大きく飛び越える) で
    強制決済させ、口座を壊滅的なドローダウン (数十%) に陥らせる。その後の
    2 件目の open 提案は kill switch (および同時に daily loss も) の
    ガードで gate_rejected になり、orders テーブルに行が増えない
    (orders.insert は gate 受理後にしか呼ばれない — 執行順序は F4 のとおり
    正しく tick → pending 執行)。
    """
    hist = _conn(tmp_path)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6, l=148.4,
                    c=148.5) for i in range(60)]                # 12:00-12:59 (bucket1 評価用)
    rows.append(_row_at(WED + timedelta(hours=1), o=148.5, h=148.6,
                        l=148.4, c=148.5))                       # 13:00 — entry quote
    rows.append(_row_at(WED + timedelta(hours=1, minutes=1),
                        o=100.0, h=100.05, l=99.90, c=100.0))    # 13:01 — 巨大ギャップ暴落
    rows.append(_row_at(WED + timedelta(hours=2), o=100.0, h=100.05,
                        l=99.95, c=100.0))    # 14:00 — 2 件目 entry quote 用
    ohlcv.import_history_bars(hist, rows, source="dukascopy")

    calls: list = []

    def source(bar):
        calls.append(bar.ts)
        if len(calls) == 1:
            return dict(OPEN_MARKET)          # sl=148.30, tp=149.00 (最初の建玉)
        if len(calls) == 2:
            return {"action": "open", "pair": "USDJPY", "direction": "long",
                    "entry_type": "market", "horizon": "day",
                    "stop_loss": 98.50, "take_profit": 103.00,
                    "reasoning": "f1b-after-crash"}
        return None

    res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                     start=WED, end=WED + timedelta(hours=3),
                     intent_source=source, eval_timeframe="1h",
                     history_conn=hist)
    assert len(calls) == 2  # 2 バケット (13:00, 14:00) とも評価はされた
    # 1 件目は暴落で SL 強制決済 (realized_pnl が大きく負)。
    assert len(res.orders) == 1
    assert res.orders[0]["close_reason"] == "sl"
    assert res.orders[0]["realized_pnl"] < -10_000
    # 2 件目は kill switch (ドローダウン) で gate_rejected — orders は
    # 増えない (accepted の場合のみ orders.insert が呼ばれるため)。
    initial_balance = SETTINGS.backtest.initial_balance
    assert res.orders[0]["realized_pnl"] < -(initial_balance * 0.02)


# ---------------------------------------------------------------------------
# Step 2 (brief 裁定 codex C3): 実装前の前提検証 — 現行 Scheduler.tick は
# mark-to-market 失敗時に early return し、_expire_limits / _force_close_day
# に到達しない可能性がある。bars_fn が常に None を返す状態でも期限切れの
# pending_fill が expired へ遷移するかを実測する。
#
# fix round 1 訂正 (sonnet I4): 元のプローブ (brief Step2 の逐語どおり —
# PENDING_FILL の指値のみ、OPEN ポジション無し) は、`_evaluate_positions`
# の `stale` フラグが `orders.list_by_status(conn, S.OPEN)` をループして
# のみ立つため、OPEN ポジションが無ければ bars_fn=None でも stale は
# 一度も True にならず (`_expire_limits` 自体も bars_fn を参照しない)、
# 「bars_fn=None の効果」を実質何も検証していなかった (report.md の記述は
# 実測範囲を超えて主張していた — この点も訂正する)。
#
# 訂正後の実測: OPEN ポジションを 1 件作った上で bars_fn=None にすると、
# `_mark_to_market` は `_evaluate_positions` の `stale=True` を実際に通り
# (record_snapshot はスキップされ、account_snapshots の最新 ts は不変)、
# それでも tick() の early return は発火せず `_expire_limits` に到達して
# 期限切れの別注文が expired になることを確認した。よって tick 内部の
# 改修は不要という結論は変わらないが、根拠は今回のテストで正しく担保する。
# ---------------------------------------------------------------------------

def test_expire_limits_reached_despite_stale_mark_to_market(tmp_path):
    conn = connect(tmp_path / "sched.db")
    init_db(conn)
    record_snapshot(conn, now=WED, balance=1_000_000, equity=1_000_000)
    state = StateStore(tmp_path / "state.json")
    clock = FixedClock(WED + timedelta(minutes=1))
    quote = Quote("USDJPY", 148.49, 148.51, WED, "test")
    spec = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000,
                          "USD", "JPY")

    def rate_fn(ccy, account_ccy, now):
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (now,))
        return ConversionRate(quote.ask, ccy, account_ccy, (now,))

    activity = ActivityLog(tmp_path / "a.log")
    executor = Executor(
        conn=conn, broker=PaperBroker(conn, SETTINGS, clock),
        settings=SETTINGS, state_store=state, activity=activity,
        notifier=Notifier(False, None), clock=clock,
        quote_fn=lambda p: quote, spec_fn=lambda p: spec, rate_fn=rate_fn)

    # OPEN ポジション 1 件 (market) — _evaluate_positions の stale=True
    # 分岐を実際に通す対象。
    market_intent = TradeIntent.from_llm_dict(dict(OPEN_MARKET),
                                              origin=Origin.SCHEDULER)
    mid1 = missions.start(conn, "trade", "local", "m1", WED)
    open_oid = executor.handle_intent(market_intent, mid1)["order_id"]
    assert orders.get(conn, open_oid)["status"] == "open"

    # 期限切れの pending_fill も別途作る (_expire_limits の到達対象)。
    limit_intent = TradeIntent.from_llm_dict(dict(OPEN), origin=Origin.SCHEDULER)
    mid2 = missions.start(conn, "trade", "local", "m2", WED)
    pend_oid = executor.handle_intent(limit_intent, mid2)["order_id"]
    assert orders.get(conn, pend_oid)["status"] == "pending_fill"
    orders.update_fields(conn, pend_oid, now=WED,
                         expires_at=(WED - timedelta(minutes=1)).isoformat())

    sched = Scheduler(
        conn=conn, executor=executor, settings=SETTINGS, state_store=state,
        activity=activity, bars_fn=lambda p: None,   # 常に None — stale 経路
        on_trade_mission=lambda reason: None, on_news_cycle=lambda: None,
        on_econ_cycle=lambda: None)

    before = snapshots.latest(conn)["ts"]
    tick_now = WED + timedelta(minutes=1)
    sched.tick(tick_now)

    after = snapshots.latest(conn)["ts"]
    assert after == before  # stale=True — この tick の snapshot は記録されない
    assert orders.get(conn, pend_oid)["status"] == "expired"  # にも関わらず到達


# ---------------------------------------------------------------------------
# 段階 1 レビュー是正 6a: _RecordingActivity.write の no-throw 契約 + 記録
# 失敗を注入しても replay (orders/SL/TP) が no-op 時と一致すること。
# ---------------------------------------------------------------------------


def test_recording_activity_write_never_raises_on_malformed_input(tmp_path):
    """`ActivityLog.write` の「決して送出しない」契約 (activity.py docstring)
    と同じく、`_RecordingActivity.write` も summary=None や category が
    `.value` を持たない素の str でも例外を送出しない。"""
    from agentic_fx.backtest.runner import _RecordingActivity, ReplayClock
    activity = _RecordingActivity(ReplayClock(WED))
    # 例外を送出しないことが主眼 — category に `.value` が無い素の
    # str/object でも `getattr(category, "value", str(category))` の
    # fallback で安全に記録される。
    activity.write("not-a-category", "evt", "ok1")
    activity.write(object(), "evt", "ok2")
    assert len(activity.entries) == 2
    assert activity.entries[0]["category"] == "not-a-category"

    class _Explodes:
        def __str__(self):
            raise RuntimeError("boom: str() itself fails")

    # `getattr(category, "value", str(category))` が str() 呼び出しで例外を
    # 送出しても write は握り潰す (try が組み立て全体を包む契約)。
    activity.write(_Explodes(), "evt", "ok3")
    assert len(activity.entries) == 2  # 例外側は記録されない (握り潰され黙って戻る)


def test_recording_activity_write_failure_does_not_change_replay_outcome(
        tmp_path, monkeypatch):
    """記録失敗 (_RecordingActivity.write が例外を握り潰す経路) を注入して
    も、orders/SL/TP の判定は記録成功時 (no-op) と完全に一致する — activity
    は可観測性のみで判断に影響しない契約 (段階 1 レビュー是正 6a)。"""
    hist = _conn(tmp_path)
    _seed_history(hist)

    def source(bar):
        if bar.ts == WED + timedelta(hours=1):
            return dict(OPEN_MARKET)
        return None

    baseline = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                          start=WED, end=WED + timedelta(hours=2),
                          intent_source=source, eval_timeframe="1h",
                          history_conn=hist)

    from agentic_fx.backtest.runner import _RecordingActivity

    def _broken_write(self, category, event, summary, ref_id=None):
        try:
            raise RuntimeError("boom: simulated recording failure")
        except Exception:
            return
    monkeypatch.setattr(_RecordingActivity, "write", _broken_write)

    hist2 = _conn(tmp_path)
    _seed_history(hist2)
    injected = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
                          start=WED, end=WED + timedelta(hours=2),
                          intent_source=source, eval_timeframe="1h",
                          history_conn=hist2)

    def _order_shape(res):
        return [(o["status"], o.get("close_reason"), o.get("stop_loss"),
                 o.get("take_profit")) for o in res.orders]

    assert _order_shape(injected) == _order_shape(baseline)
    assert injected.equity_curve == baseline.equity_curve
    # 記録失敗を注入したため activity 由来の kill_switch_events は取れない
    # (StateStore 由来は影響を受けない) — replay 自体は止まらないことが主眼。


# ---------------------------------------------------------------------------
# 段階 1 レビュー是正 6c: kill_switch_events 境界テスト
# ---------------------------------------------------------------------------


def _snap(conn, *, ts, equity, hwm, balance=None):
    conn.execute(
        "INSERT INTO account_snapshots (ts, balance, equity, hwm, cashflow, "
        "source) VALUES (?,?,?,?,0,'paper')",
        (ts, balance if balance is not None else equity, equity, hwm))
    conn.commit()


def test_kill_switch_event_picks_max_id_snapshot_at_same_timestamp(tmp_path):
    """event と同時刻に equity/hwm が異なる複数 snapshot があるとき、最大
    id (= 最後に書かれたもの) を選ぶ。"""
    from agentic_fx.backtest.runner import _kill_switch_event
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    ts = WED.isoformat()
    _snap(conn, ts=ts, equity=90_000.0, hwm=100_000.0)   # id=1, dd=10%
    _snap(conn, ts=ts, equity=80_000.0, hwm=100_000.0)   # id=2 (最新) dd=20%
    event = _kill_switch_event(conn, {"ts": ts, "text": "latched"}, "latched")
    assert event["drawdown_pct"] == pytest.approx(20.0)


def test_kill_switch_event_hwm_non_positive_yields_null_drawdown(tmp_path):
    """hwm<=0 (未初期化・境界) は drawdown_pct=None (計算不能)。"""
    from agentic_fx.backtest.runner import _kill_switch_event
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    ts = WED.isoformat()
    _snap(conn, ts=ts, equity=1_000.0, hwm=0.0)
    event = _kill_switch_event(conn, {"ts": ts, "text": "latched"}, "latched")
    assert event["drawdown_pct"] is None


def test_kill_switch_event_equity_above_hwm_clamps_drawdown_to_zero(tmp_path):
    """段階 2 レビュー是正 c2a-09: `max(0.0, …)` の下限。equity > hwm (新高値
    更新直後・浮動小数の丸め等) で生の計算式は負値になるが、
    drawdown_pct は 0.0 にクランプされる (負の drawdown は意味を持たない)。
    """
    from agentic_fx.backtest.runner import _kill_switch_event
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    ts = WED.isoformat()
    _snap(conn, ts=ts, equity=100_500.0, hwm=100_000.0)  # equity > hwm
    event = _kill_switch_event(conn, {"ts": ts, "text": "latched"}, "latched")
    assert event["drawdown_pct"] == 0.0


def test_kill_switch_event_reset_reports_released_kind(tmp_path):
    """StateStore 由来の reset 遷移 (`_RecordingStateStore`) は "released" と
    して観測される — `run_replay` の合流規則 (kind に reset を含めば
    released) を `_RecordingStateStore.kill_switch_transitions` の実データで
    直接確認する。"""
    from agentic_fx.backtest.runner import _RecordingStateStore, ReplayClock
    clock = ReplayClock(WED)
    state = _RecordingStateStore(tmp_path / "state.json", clock=clock)
    state.update(kill_switch_latched=True)
    state.update(kill_switch_latched=False)
    kinds = [t["kind"] for t in state.kill_switch_transitions]
    assert kinds == ["latched", "released"]

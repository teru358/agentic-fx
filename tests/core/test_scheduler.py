import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core import market_hours
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Bar, ConversionRate, FixedClock, InstrumentSpec, Mode, Origin, Quote,
    TradeIntent,
)
from agentic_fx.core.executor import Executor, has_unresolved_unknown
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)  # 水曜
FRI = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)  # 金曜 正午
SAT = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000,
                      base_currency="USD", quote_currency="JPY")
# 既存の site1 系テストは pair 文字列 (GBPJPY/EURUSD) をラベルとして使うだけで
# spec_fn は常に SPEC (USDJPY) を返すスタブに依存している (通貨とは無関係な
# 例外隔離テストのため)。EUR_SPEC/GBP_SPEC は換算層の新規テストが明示的に
# spec_fn を差し替えるときのみ使う (Env のデフォルトは変更しない)。
EUR_SPEC = InstrumentSpec("EURUSD", 0.0001, 0.01, 50.0, 0.01, 100_000,
                          base_currency="EUR", quote_currency="USD")
GBP_SPEC = InstrumentSpec("GBPJPY", 0.01, 0.01, 50.0, 0.01, 100_000,
                          base_currency="GBP", quote_currency="JPY")
QUOTE = Quote("USDJPY", 148.49, 148.51, WED, "test")


def _default_rate_fn(ccy, account_ccy, now):
    """既存テスト (USDJPY のみ想定) 向けの最小限のレートスタブ。

    spec_fn は常に SPEC (USDJPY: quote=JPY/base=USD) を返す既存の慣習
    (site1 系テストのコメント参照) なので、このスタブは JPY 恒等と
    USD->JPY (QUOTE.ask 固定) だけを知っていれば足りる。換算層自体の
    新規テストは `env.executor.rate_fn` を明示的に差し替える。
    """
    if ccy == account_ccy:
        return ConversionRate(1.0, ccy, account_ccy, (now,))
    if ccy == "USD" and account_ccy == "JPY":
        return ConversionRate(QUOTE.ask, "USD", "JPY", (now,))
    raise DataUnhealthy(f"no rate for {ccy}->{account_ccy}")


class Env:
    def __init__(self, tmp_path, quote_fn=None, base=WED, news_fn=None,
                 econ_fn=None, rate_fn=None):
        from agentic_fx.core.notifier import Notifier
        self.conn = connect(tmp_path / "t.db")
        init_db(self.conn)
        record_snapshot(self.conn, now=base, balance=1_000_000, equity=1_000_000)
        self.state = StateStore(tmp_path / "state.json")
        self.tmp_path = tmp_path
        self.bars: dict[str, Bar] = {}
        self.trade_calls = 0
        self.news_calls = 0
        self.econ_calls = 0
        self._news_fn = news_fn
        self._econ_fn = econ_fn
        self.base = base
        clock = FixedClock(base)
        self.executor = Executor(
            conn=self.conn, broker=PaperBroker(self.conn, SETTINGS, clock),
            settings=SETTINGS, state_store=self.state,
            activity=ActivityLog(tmp_path / "a.log"),
            notifier=Notifier(enabled=False, webhook_url=None), clock=clock,
            quote_fn=quote_fn or (lambda p: QUOTE), spec_fn=lambda p: SPEC,
            rate_fn=rate_fn or _default_rate_fn)
        self.sched = Scheduler(
            conn=self.conn, executor=self.executor, settings=SETTINGS,
            state_store=self.state, activity=ActivityLog(tmp_path / "a.log"),
            bars_fn=lambda p: self.bars.get(p),
            on_trade_mission=self._trade, on_news_cycle=self._news,
            on_econ_cycle=self._econ)

    def _trade(self):
        self.trade_calls += 1

    def _news(self):
        self.news_calls += 1
        if self._news_fn is not None:
            self._news_fn()

    def _econ(self):
        self.econ_calls += 1
        if self._econ_fn is not None:
            self._econ_fn()

    def place_limit(self, price=148.20, sl=147.80, tp=149.00, hours=4):
        it = TradeIntent.from_llm_dict(
            {"action": "open", "pair": "USDJPY", "direction": "long",
             "entry_type": "limit", "horizon": "day", "limit_price": price,
             "expires_in": f"{hours}h", "stop_loss": sl, "take_profit": tp,
             "reasoning": "t"}, origin=Origin.SCHEDULER)
        mid = missions.start(self.conn, "trade", "local", "m", self.base)
        return self.executor.handle_intent(it, mid)["order_id"]


def test_hourly_trade_mission(tmp_path):
    env = Env(tmp_path)
    env.sched.tick(WED)
    assert env.trade_calls == 1
    env.sched.tick(WED + timedelta(minutes=30))
    assert env.trade_calls == 1  # まだ 1 時間経っていない
    env.sched.tick(WED + timedelta(hours=1))
    assert env.trade_calls == 2


def test_market_closed_runs_data_cycles_but_no_trade_mission(tmp_path):
    """非退行 (旧 test_market_closed_only_news): クローズ中は Mission を
    回さないが、ニュース / econ の収集サイクルは従来どおり回る。

    改名の理由: cross-plan 修正で news / econ は開場中も回るようになった
    ため、「クローズ中**だけ** news」を意味する旧名は誤りになった。"""
    env = Env(tmp_path)
    assert not market_hours.is_market_open(SAT)
    env.sched.tick(SAT)
    assert env.trade_calls == 0
    assert env.news_calls == 1
    assert env.econ_calls == 1
    env.sched.tick(SAT + timedelta(minutes=10))
    assert env.news_calls == 1  # 30 分間隔
    env.sched.tick(SAT + timedelta(minutes=31))
    assert env.news_calls == 2


# --- cross-plan 修正①: ニュース / econ は開場中も回る -------------------

def test_news_and_econ_cycles_run_while_market_open(tmp_path):
    """欠陥①の直接のピン。

    旧実装は `on_news_cycle` の呼び出しが `if not open_now:` ブロック内の
    1 箇所だけで、そのブロックは直後に return していた。市場は金 17:00 NY
    〜日 17:00 NY しか閉じないため、ニュース収集 (と RAG の 48h 掃除) は
    **週末しか走らなかった** (14 日実測で news cycle は全部週末、最大間隔
    5 日)。econ も呼び出し口自体が存在しなかった。"""
    env = Env(tmp_path)
    assert market_hours.is_market_open(WED)
    env.sched.tick(WED)
    assert env.news_calls == 1
    assert env.econ_calls == 1


def test_news_interval_is_consistent_across_market_boundary(tmp_path):
    """_NEWS_INTERVAL の間隔判定が開場・閉場をまたいで一貫すること
    (閉場中に呼んだ直後に開場しても 30 分経つまで再度呼ばない)。"""
    t_closed = datetime(2026, 7, 26, 20, 50, tzinfo=timezone.utc)  # 日 閉場
    t_open = t_closed + timedelta(minutes=20)   # 日 21:10 UTC = NY 17:10 開場
    t_later = t_closed + timedelta(minutes=35)
    assert not market_hours.is_market_open(t_closed)
    assert market_hours.is_market_open(t_open)

    env = Env(tmp_path, base=t_closed)
    env.sched.tick(t_closed)
    assert env.news_calls == 1
    env.sched.tick(t_open)          # 開場したが 20 分しか経っていない
    assert env.news_calls == 1
    env.sched.tick(t_later)
    assert env.news_calls == 2


def test_econ_interval_is_six_hours_and_consistent_across_boundary(tmp_path):
    """_ECON_INTERVAL (6 時間) が開場・閉場をまたいで一貫すること。

    6 時間: ForexFactory は**週次**の JSON を返すので 30 分ごとは無駄だが、
    当日の forecast/previous は更新されうるので 1 日 1 回よりは細かくする。"""
    t_closed = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)   # 日 閉場
    t_open = t_closed + timedelta(hours=3, minutes=10)             # 開場後
    t_later = t_closed + timedelta(hours=6, minutes=10)
    assert not market_hours.is_market_open(t_closed)
    assert market_hours.is_market_open(t_open)
    assert market_hours.is_market_open(t_later)

    env = Env(tmp_path, base=t_closed)
    env.sched.tick(t_closed)
    assert env.econ_calls == 1
    env.sched.tick(t_open)          # 6 時間経っていない
    assert env.econ_calls == 1
    env.sched.tick(t_later)
    assert env.econ_calls == 2


def test_scheduler_requires_on_econ_cycle_without_default(tmp_path):
    """`on_econ_cycle` にデフォルト値を与えないことのピン。

    デフォルトの no-op を与えると「呼び忘れ」がまさに欠陥② (EconCalendar.
    refresh の呼び出し元がどこにも無く econ_events が永久に空) として再発
    する。デフォルトが無ければ、未配線の呼び出し元をテストが列挙してくれる。
    他の必須引数を落として TypeError を得る書き方だとデフォルトの有無を
    区別できないので、**完全な kwargs から on_econ_cycle だけを抜く**。"""
    env = Env(tmp_path)
    kwargs = dict(
        conn=env.conn, executor=env.executor, settings=SETTINGS,
        state_store=env.state, activity=ActivityLog(tmp_path / "a.log"),
        bars_fn=lambda p: None, on_trade_mission=lambda: None,
        on_news_cycle=lambda: None, on_econ_cycle=lambda: None)
    assert Scheduler(**kwargs) is not None      # 完全な kwargs では構築できる
    kwargs.pop("on_econ_cycle")
    with pytest.raises(TypeError, match="on_econ_cycle"):
        Scheduler(**kwargs)


# --- cross-plan 修正②: データ経路の障害で資金保護を止めない -------------

def _boom(msg="hook down"):
    def _f():
        raise RuntimeError(msg)
    return _f


def test_news_cycle_exception_does_not_stop_fill_and_exit_monitoring(tmp_path):
    """修正 2 の核心: news cycle は tick の冒頭 (= _process_exits より前)
    に移ったので、ニュース経路の障害が SL/TP 監視 (資金保護) を止めては
    ならない。例外が起きた**その tick**で約定処理と SL 判定が走ること。"""
    env = Env(tmp_path, news_fn=_boom("news down"))
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert env.news_calls == 1                     # 例外を投げた
    assert orders.get(env.conn, oid)["status"] == "open"
    # 30 分後 = news が再び呼ばれる tick で SL に到達させる
    later = WED + timedelta(minutes=31)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", later, 147.60, 147.65, 147.50,
                             147.55, 100)
    env.sched.tick(later)
    assert env.news_calls == 2
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "sl"


def test_econ_cycle_exception_does_not_stop_fill_and_exit_monitoring(tmp_path):
    env = Env(tmp_path, econ_fn=_boom("econ down"))
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert env.econ_calls == 1
    assert orders.get(env.conn, oid)["status"] == "open"
    # 6 時間後 = econ が再び呼ばれる tick で SL に到達させる
    later = WED + timedelta(hours=6, minutes=1)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", later, 147.60, 147.65, 147.50,
                             147.55, 100)
    env.sched.tick(later)
    assert env.econ_calls == 2
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "sl"


def test_hook_exception_does_not_stop_market_close_cancellation(tmp_path):
    """クローズ移行時の未約定実指値の取消 (監視外に残さない = 資金保護) も、
    冒頭に移った news / econ の障害で止まってはならない。"""
    env = Env(tmp_path, base=FRI, news_fn=_boom(), econ_fn=_boom())
    oid = env.place_limit(hours=12)
    env.state.update(mode=Mode.TRADING)
    fri_2101 = datetime(2026, 7, 24, 21, 1, tzinfo=timezone.utc)
    env.sched.tick(fri_2101)
    assert env.news_calls == 1 and env.econ_calls == 1   # 両方が例外を投げた
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_hook_failure_is_recorded_in_activity_and_log_without_secrets(
        tmp_path, caplog):
    """無音で握り潰さない: 技術ログ (warning) と activity の**両方**に残る。
    メッセージは safe_error_text を通し、URL / 秘密を出さない。"""
    import httpx

    def news_boom():
        raise httpx.ConnectError(
            "connect failed: https://news.example.com/feed?apikey=SECRET")

    def econ_boom():
        raise RuntimeError("econ down (apikey=SECRET)")

    env = Env(tmp_path, news_fn=news_boom, econ_fn=econ_boom)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.scheduler"):
        env.sched.tick(WED)
    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert "news_cycle_error" in act
    assert "econ_cycle_error" in act
    both = act + caplog.text
    assert "news cycle failed" in caplog.text
    assert "econ cycle failed" in caplog.text
    assert "SECRET" not in both                  # 秘密は伏字
    assert "news.example.com" not in both        # httpx 由来は URL を出さない
    assert "ConnectError" in both                # 型名は診断のため残す
    assert "RuntimeError" in both


def test_failed_hook_still_advances_the_interval(tmp_path):
    """失敗しても `_last_news` / `_last_econ` を進める (毎 tick 再試行しない)。

    根拠: hook は _process_exits (資金保護) より前に立つので、**遅い失敗**
    (HTTP タイムアウト) を毎 tick 再試行すると、その遅延を毎 tick 資金保護
    に負わせることになる。ニュース / econ は fail-open なので、間隔を空けて
    再試行するほうが安全側。"""
    env = Env(tmp_path, news_fn=_boom(), econ_fn=_boom())
    env.sched.tick(WED)
    assert (env.news_calls, env.econ_calls) == (1, 1)
    env.sched.tick(WED + timedelta(minutes=1))
    assert (env.news_calls, env.econ_calls) == (1, 1)   # 即座に再試行しない
    env.sched.tick(WED + timedelta(minutes=31))
    assert (env.news_calls, env.econ_calls) == (2, 1)
    env.sched.tick(WED + timedelta(hours=6, minutes=1))
    assert (env.news_calls, env.econ_calls) == (3, 2)


def test_limit_expiry_cancelled(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(hours=4)
    env.sched.tick(WED + timedelta(hours=5))
    assert orders.get(env.conn, oid)["status"] == "expired"


def test_limit_fill_then_sl(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["status"] == "open"
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.10, 148.15, 147.70, 147.75, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "sl"
    assert row["realized_pnl"] < 0


def test_stale_or_duplicate_bar_ignored(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    # 古いバー (10 分前) は fills に使わない
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED - timedelta(minutes=10),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["status"] == "pending_fill"
    # 同一バーの再処理はしない
    fresh = Bar("USDJPY", "1m", WED + timedelta(minutes=2), 148.40, 148.45,
                148.30, 148.40, 100)  # 指値未到達
    env.bars["USDJPY"] = fresh
    env.sched.tick(WED + timedelta(minutes=2))
    env.sched.tick(WED + timedelta(minutes=3))  # 同じ bar.ts → skip (副作用なし)
    assert orders.get(env.conn, oid)["status"] == "pending_fill"


def test_day_forced_close_uses_quote_side(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    near_close = WED.replace(hour=20, minute=57)
    env.sched.tick(near_close)
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "day_rollover"
    assert row["close_price"] == QUOTE.bid  # long は bid (保守側)


def test_day_close_deferred_on_quote_failure(tmp_path):
    def bad_quote(pair):
        raise RuntimeError("quote down")

    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    env.executor.quote_fn = bad_quote
    near_close = WED.replace(hour=20, minute=57)
    env.sched.tick(near_close)
    row = orders.get(env.conn, oid)
    assert row["status"] == "open"  # 架空価格で閉じない (次 tick 再試行)
    assert "day_close_deferred" in (env.tmp_path / "a.log").read_text(
        encoding="utf-8")


def test_reservation_maintenance_cancels_limit(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    # 口座が悪化 (balance 300,000): mark-to-market が equity 300,000 を記録し、
    # 総リスク上限 1.5% = 4,500 < 予約リスク ≈4,920 → 約定前に自動取消
    env.executor.broker.equity = lambda: (300_000.0, 300_000.0)
    env.sched.tick(WED + timedelta(minutes=1))
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "reservation"


def test_reconcile_resolves_unknown(tmp_path):
    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="cancel_unknown", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8)
    env.sched.tick(WED)
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_mark_to_market_snapshot(tmp_path):
    from agentic_fx.store import snapshots
    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # fill @148.20
    # 含み損: バー close 147.9 → (147.9-148.2)*100000*qty
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             147.95, 147.98, 147.90, 147.92, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    snap = snapshots.latest(env.conn)
    assert snap["equity"] < 1_000_000  # unrealized 込み


def test_close_transition_cancels_trading_limits(tmp_path):
    # レビュー修正 9: 元は WED 基準の Env に orders.update_fields で expires_at
    # を直接書き換えて回避していたが、それは gate (limit_expiry_max_h<=24h) を
    # 経由しないテスト専用の裏道だった。正しい直し方はフィクスチャの基準時刻
    # 自体を金曜にすること — hours=12 (gate の 24h 制約内) で
    # expires_at = 金 12:00+12h = 土 00:00 となり、金 21:01 のクローズ移行
    # tick 時点でまだ pending_fill のまま検証対象に到達する。
    env = Env(tmp_path, base=FRI)
    oid = env.place_limit(hours=12)
    env.state.update(mode=Mode.TRADING)
    fri_2059 = datetime(2026, 7, 24, 20, 59, tzinfo=timezone.utc)
    fri_2101 = datetime(2026, 7, 24, 21, 1, tzinfo=timezone.utc)
    env.sched.tick(fri_2059)             # open 中
    env.sched.tick(fri_2101)             # クローズ移行 tick
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_day_forced_close_on_friday_2057(tmp_path):
    """申し送り事項: 金曜 20:57 で day 強制クローズが正しく発火することの
    直接検証 (既存の test_day_forced_close_uses_quote_side は WED=水曜 基準
    だったため、金曜ケースは未検証だった — レビュー修正 9)。"""
    env = Env(tmp_path, base=FRI)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", FRI, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(FRI + timedelta(minutes=1))  # fill
    fri_near_close = FRI.replace(hour=20, minute=57)
    env.sched.tick(fri_near_close)
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "day_rollover"
    assert row["close_price"] == QUOTE.bid


def test_same_bar_tp_not_awarded_by_second_loop(tmp_path):
    """レビュー修正 1 (Critical): _process_fills の第 2 ループが、同一 tick
    で第 1 ループが fill させた注文を再取得し、entry_same_bar=False で
    再評価してしまうと、本来「同一バー内の順序判定不能」として見送られる
    べき TP が確定してしまう。"""
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    # 指値到達 (low<=148.20) と同時に TP (149.00) にもギャップで到達するバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 149.20, 148.15,
                             149.00, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    row = orders.get(env.conn, oid)
    assert row["status"] == "open"  # 同一バーでは TP 確定させない
    assert row["close_reason"] is None


def test_closing_retried_and_blocks_gate_until_resolved(tmp_path):
    """レビュー修正 2 (Critical): CLOSE_UNKNOWN -> CLOSING に遷移した後、
    quote 障害で一度失敗しても CLOSING は次 tick 以降も再走査され、quote が
    復旧すれば CLOSED まで解決する。また解決するまでの間、
    has_unresolved_unknown が True であり続け (executor._UNKNOWN に CLOSING
    を含めた効果)、gate の新規発注停止が維持される。"""
    calls = {"n": 0}

    def flaky_quote(pair):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("quote down")
        return QUOTE

    env = Env(tmp_path, quote_fn=flaky_quote)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="close_unknown", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        avg_fill_price=148.2)
    env.sched.tick(WED)  # reconcile -> CLOSING, quote 失敗で再試行は次 tick へ
    row = orders.get(env.conn, oid)
    assert row["status"] == "closing"
    assert has_unresolved_unknown(env.conn)  # gate は新規発注を止め続ける

    env.sched.tick(WED + timedelta(minutes=1))  # quote 復旧 → 再試行で解決
    row2 = orders.get(env.conn, oid)
    assert row2["status"] == "closed"
    assert not has_unresolved_unknown(env.conn)


def test_closing_retry_db_failure_is_not_swallowed(tmp_path):
    """レビュー修正 2 の項目 3: broker.close 成功後の DB 確定処理
    (compute_pnl/transitions.transition) が失敗した場合、quote 障害と
    同じように黙って握りつぶしてはいけない (broker 側は既に閉じているかも
    しれず、不整合を検知できなくなるため)。"""
    from agentic_fx.core import scheduler as scheduler_mod

    env = Env(tmp_path)
    orders.insert(env.conn, pair="USDJPY", direction="long",
                 entry_type="limit", horizon="day",
                 status="close_unknown", now=WED, quantity=0.1,
                 requested_price=148.2, stop_loss=147.8, avg_fill_price=148.2)

    orig_transition = scheduler_mod.transitions.transition

    def flaky_transition(conn, order_id, to, now, **fields):
        if to.value == "closed":
            raise RuntimeError("db write failed")
        return orig_transition(conn, order_id, to, now, **fields)

    scheduler_mod.transitions.transition = flaky_transition
    try:
        with pytest.raises(RuntimeError, match="db write failed"):
            env.sched.tick(WED)
    finally:
        scheduler_mod.transitions.transition = orig_transition


def test_restart_with_first_tick_closed_still_cancels_trading_limits(tmp_path):
    """レビュー修正 4 (Medium): サービスが市場オープン中に落ち、クローズ後に
    再起動すると、最初の tick で _was_open は None (True でも False でもない)
    になる。この場合も取引モードの未約定実指値は監視外に残さない。"""
    env = Env(tmp_path)
    oid = env.place_limit()
    env.state.update(mode=Mode.TRADING)
    env.sched.tick(SAT)  # 再起動後いきなりクローズ中に最初の tick が来るケース
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_processed_bar_marking_covers_pairs_without_tracked_orders(tmp_path):
    """レビュー修正 5: _processed_bar_ts のマーキングが従来
    (PENDING_FILL/OPEN/CLOSED/CANCELLED のいずれかが存在する pair のみ) だと、
    ある tick でそれら 4 状態のどれも無い pair のバーはマーキングされない。
    settings.pairs を直接走査してマーキングすることで、この抜けを塞ぐ。"""
    env = Env(tmp_path)
    bar = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15, 148.25, 100)
    env.bars["USDJPY"] = bar
    # この tick 時点では USDJPY に一切注文が無い (4 状態のどれにも該当しない)
    env.sched.tick(WED + timedelta(minutes=1))
    # フィード停止を模して同一バーのまま、直後に新規指値を出す
    oid = env.place_limit()  # entry=148.20 (bar の high/low 内 → 約定しうる)
    env.sched.tick(WED + timedelta(minutes=2))
    # 直前 tick で「処理済み」としてマーキングされた同一バーでは約定させない
    assert orders.get(env.conn, oid)["status"] == "pending_fill"


def test_reservation_maintenance_zero_equity_no_crash(tmp_path):
    """レビュー修正 6: equity<=0 では notional/equity がゼロ除算になる。

    codex C-I1 で期待値を更新した。旧版は
    `assert row["status"] == "pending_fill"  # gate 側の fail closed に委ねる`
    で、**欠陥をテストが固定していた** — gate は「新規発注」時にしか走らず、
    既に予約済みの指値が約定する経路 (`_process_limit_fills`) では再実行
    されないので、equity<=0 (債務超過) のまま到達バーが来れば OPEN が
    生まれてしまう。equity<=0 は口座 snapshot 欠損と同等以上に扱い、
    全 pending_fill を取消す。ゼロ除算にならないことの検証はそのまま。
    """
    env = Env(tmp_path)
    oid = env.place_limit()
    env.executor.broker.equity = lambda: (0.0, 0.0)
    env.sched.tick(WED + timedelta(minutes=1))  # ZeroDivisionError にならない
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "equity_nonpositive"


# --- codex C-I1: equity<=0 でも予約済み指値が約定してしまう ---------------

def test_zero_equity_cancels_pending_and_never_opens_but_exits_still_run(tmp_path):
    """債務超過 (equity<=0) の tick で

    1. 到達バーがあっても pending_fill から OPEN が生まれないこと
    2. それでも既存 OPEN の SL/TP 監視 (資金保護) は必ず走ること

    を 1 本のバーで同時に検証する。旧実装は `_maintain_reservations` が
    equity<=0 で早期 return し「gate の fail closed に委ねる」としていたが、
    gate は予約済み指値の約定時には再実行されない。
    """
    env = Env(tmp_path)
    pending = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    # 既存 OPEN (swing — _force_close_day の対象外にして経路を混ぜない)
    open_id = orders.insert(env.conn, pair="USDJPY", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=148.20, stop_loss=147.80,
                            take_profit=149.00)
    env.executor.broker.equity = lambda: (0.0, 0.0)
    # 指値 (148.20) に到達し、かつ SL (147.80) も割るバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    pending_row = orders.get(env.conn, pending)
    assert pending_row["status"] != "open"          # 債務超過で OPEN を作らない
    assert pending_row["status"] == "cancelled"
    assert pending_row["close_reason"] == "equity_nonpositive"
    open_row = orders.get(env.conn, open_id)
    assert open_row["status"] == "closed"           # 資金保護は止めない
    assert open_row["close_reason"] == "sl"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "equity_nonpositive_cancel_pending" in log


def test_zero_equity_skips_limit_fills_even_if_cancellation_is_neutralized(
        tmp_path, monkeypatch):
    """2 層目 (fills スキップ) の単独ピン。

    1 層目 (全 pending 取消) が効いていると pending_fill が空になるため、
    「`_process_limit_fills` のスキップ条件から equity<=0 を外す」変異は
    上のテストでは検知できない (取消でマスクされる)。取消を無効化した
    状態で、なお約定しないことを直接固定する。
    """
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    monkeypatch.setattr(env.sched, "_cancel_all_pending",
                        lambda *a, **k: None)
    env.executor.broker.equity = lambda: (0.0, 0.0)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["status"] == "pending_fill"  # OPEN ではない


def test_mark_to_market_skips_snapshot_on_stale_bar_but_tick_continues(tmp_path):
    """レビュー修正 7: open ポジションのバーが陳腐化している場合、
    その tick の snapshot 記録は見送る (古い価格で kill switch を誤判定
    させないため) が、tick 自体は継続する。"""
    from agentic_fx.store import snapshots

    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # fill -> OPEN
    snap_before = snapshots.latest(env.conn)
    # バーを更新しないまま (フィード停止) 鮮度切れになるまで進める
    env.sched.tick(WED + timedelta(minutes=7))
    snap_after = snapshots.latest(env.conn)
    assert snap_after["ts"] == snap_before["ts"]  # 新しい snapshot は記録されない
    assert "snapshot_stale_bar_skip" in (env.tmp_path / "a.log").read_text(
        encoding="utf-8")
    assert orders.get(env.conn, oid)["status"] == "open"  # tick 自体は継続


# --- レビュー修正 (codex 1): 口座 snapshot 陳腐化時に指値を約定させない ---

def test_pending_fill_not_processed_when_account_snapshot_unknown(tmp_path):
    """current_account が陳腐化/欠損 (別ペアのバー欠落で mark_to_market の
    snapshot 記録がずっとスキップされ続け、最後の snapshot が 10 分の陳腐化
    チェックを超過するケース) の場合、_maintain_reservations が何もしない
    だけでは不十分 — 総リスク・レバレッジを再検証できない状態で pending_fill
    が約定してしまう (fail-open)。全 pending_fill を取消し、その tick の
    fills 処理をスキップしなければならない。"""
    env = Env(tmp_path)
    # 別ペア (EURUSD) の open ポジションを直接挿入。このペアのバーは一度も
    # 与えないため、mark_to_market は毎 tick 陳腐化スキップし続け、
    # snapshot (Env.__init__ 時点の WED) が更新されないまま 10 分の陳腐化
    # チェックを超過する
    orders.insert(env.conn, pair="EURUSD", direction="long",
                 entry_type="market", horizon="swing", status="open",
                 now=WED, quantity=0.1, avg_fill_price=1.1000,
                 stop_loss=1.0960, take_profit=1.1120)
    oid = env.place_limit()  # USDJPY entry=148.20
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=11),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=11))
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "account_unknown"
    assert "account_unknown" in (env.tmp_path / "a.log").read_text(
        encoding="utf-8")


# --- レビュー修正 (codex 3): day 強制決済は注文毎の期限で毎 tick 再試行 ---

def test_day_forced_close_retries_past_rollover_after_quote_recovers(tmp_path):
    """20:55-20:59 に quote 障害が続き 21:00 を過ぎると、旧実装は
    next_rollover(now) が翌日を返すため day 強制決済の対象から外れてしまい、
    day ポジションが約 24 時間 (金曜なら週末) 残ってしまっていた。注文毎の
    期限 (filled_at 基準の next_rollover) で判定すれば、期限を過ぎた後も
    quote が復旧し次第、毎 tick 再試行される。"""
    def bad_quote(pair):
        raise RuntimeError("quote down")

    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # fill (filled_at ≈ WED+1min)
    env.executor.quote_fn = bad_quote
    near_close = WED.replace(hour=20, minute=57)
    env.sched.tick(near_close)  # quote 障害で延期
    assert orders.get(env.conn, oid)["status"] == "open"

    env.executor.quote_fn = lambda p: QUOTE  # quote 復旧
    after_rollover = WED.replace(hour=21, minute=30)  # 期限 (21:00) を過ぎている
    env.sched.tick(after_rollover)
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "day_rollover"


# --- レビュー修正 (codex 4): reconcile は message="not_found" のときだけ終端化 ---

def test_reconcile_does_not_finalize_when_broker_reports_still_exists(tmp_path):
    from agentic_fx.core.contracts import BrokerResult

    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="cancel_unknown", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8)
    # 将来の broker が「照会成功・注文はまだ存在する」を ok で返すケースの再現
    env.executor.broker.reconcile = lambda row: BrokerResult(
        status="ok", message="still_open")
    env.sched.tick(WED)
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancel_unknown"  # status=="ok" だけでは終端化しない
    assert has_unresolved_unknown(env.conn)

    # message == "not_found" になれば次 tick で終端化される
    env.executor.broker.reconcile = lambda row: BrokerResult(
        status="ok", message="not_found")
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["status"] == "cancelled"


# --- 修正ラウンド 2: 口座陳腐化中も既存ポジションの SL/TP 監視は止めない ---

def test_open_position_sl_still_monitored_when_account_snapshot_unknown(tmp_path):
    """回帰修正: codex 1 の `if account is not None: self._process_fills(now)`
    は _process_fills 全体 (PENDING_FILL の約定処理 **と** OPEN の SL/TP
    監視の両方) をスキップしてしまい、口座 snapshot 陳腐化中は既存建玉の
    資金保護まで止まっていた。既存ポジションの SL/TP 監視は口座情報の有無に
    関わらず必ず動かなければならない。"""
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # fill -> open
    assert orders.get(env.conn, oid)["status"] == "open"
    # 別ペア (EURUSD) の open ポジションを挿入し、バーを一度も与えないことで
    # current_account を陳腐化させる (10 分超)
    orders.insert(env.conn, pair="EURUSD", direction="long",
                 entry_type="market", horizon="swing", status="open",
                 now=WED, quantity=0.1, avg_fill_price=1.1000,
                 stop_loss=1.0960, take_profit=1.1120)
    # SL (147.80) を明確に下回るバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=12),
                             147.60, 147.65, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=12))
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "sl"


def test_pending_fill_still_skipped_when_account_snapshot_unknown_after_split(tmp_path):
    """_process_limit_fills / _process_exits に分離した後も、修正 1 の意図
    (口座不明時は新規約定させない) が維持されていることの回帰確認。"""
    env = Env(tmp_path)
    orders.insert(env.conn, pair="EURUSD", direction="long",
                 entry_type="market", horizon="swing", status="open",
                 now=WED, quantity=0.1, avg_fill_price=1.1000,
                 stop_loss=1.0960, take_profit=1.1120)
    oid = env.place_limit()  # USDJPY entry=148.20
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=11),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=11))
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "account_unknown"


def test_reconcile_exception_skips_order_but_tick_continues(tmp_path):
    """scheduler が直接呼ぶ broker.reconcile が例外を投げても、その注文だけ
    スキップして他の注文の処理・tick 全体は継続すること。"""
    from agentic_fx.core.contracts import BrokerResult

    env = Env(tmp_path)
    oid1 = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="cancel_unknown", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8)
    oid2 = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="cancel_unknown", now=WED, quantity=0.1,
                        requested_price=148.1, stop_loss=147.7)

    def flaky_reconcile(row):
        if row["id"] == oid1:
            raise RuntimeError("reconcile down")
        return BrokerResult(status="ok", message="not_found")

    env.executor.broker.reconcile = flaky_reconcile
    env.sched.tick(WED)  # oid1 の例外で落ちず、oid2 は解決される
    assert orders.get(env.conn, oid1)["status"] == "cancel_unknown"
    assert orders.get(env.conn, oid2)["status"] == "cancelled"


def test_close_retry_broker_exception_skips_order_but_tick_continues(tmp_path):
    """_retry_close 内の直接 broker.close 呼び出しが例外を投げても、その
    注文だけスキップ (close_unknown へ, 下の修正ラウンド 3 参照) して他の
    注文の処理・tick 全体は継続すること。"""
    from agentic_fx.core.contracts import BrokerResult

    env = Env(tmp_path)
    oid1 = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="closing", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        avg_fill_price=148.2)
    oid2 = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="closing", now=WED, quantity=0.1,
                        requested_price=148.1, stop_loss=147.7,
                        avg_fill_price=148.1)

    def flaky_close(row, price, reason):
        if row["id"] == oid1:
            raise RuntimeError("close broker down")
        return BrokerResult(status="ok")

    env.executor.broker.close = flaky_close
    env.sched.tick(WED)
    # 修正ラウンド 3: broker.close の例外は CLOSING のまま次 tick 再試行
    # するのではなく、CLOSE_UNKNOWN に遷移させ reconcile 経路に乗せる
    # (broker 側は既に成功しているかもしれないため、盲目的な再試行=二重
    # クローズのリスクを避ける)。
    assert orders.get(env.conn, oid1)["status"] == "close_unknown"
    assert orders.get(env.conn, oid2)["status"] == "closed"


# --- 修正ラウンド 3: _retry_close の例外は発生源で扱いを変える ---

def test_close_retry_broker_exception_marks_close_unknown_not_closing(tmp_path):
    """broker.close() の例外は「broker 側では成功しているかもしれない」
    ため、CLOSING のまま黙って次 tick 再試行してはいけない (Phase 3 の
    MT5 で reconcile せず盲目的に再 close する二重クローズのリスクになる)。
    CLOSE_UNKNOWN に遷移させ、次 tick の reconcile 経路に乗せること。"""
    def bad_broker_close(row, price, reason):
        raise RuntimeError("broker close timeout")

    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="closing", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        avg_fill_price=148.2)
    env.executor.broker.close = bad_broker_close
    env.sched.tick(WED)
    row = orders.get(env.conn, oid)
    assert row["status"] == "close_unknown"
    assert "close_unknown" in (env.tmp_path / "a.log").read_text(
        encoding="utf-8")


def test_close_retry_quote_exception_stays_closing_for_next_tick_retry(tmp_path):
    """quote_fn の例外は broker に触れる前に起きる → 未実行が確定して
    おり、CLOSING のまま次 tick で再試行してよい (broker.close の例外とは
    区別されること — close_unknown にはならない)。"""
    calls = {"n": 0}

    def flaky_quote(pair):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("quote down")
        return QUOTE

    env = Env(tmp_path, quote_fn=flaky_quote)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="closing", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        avg_fill_price=148.2)
    env.sched.tick(WED)  # quote 障害 — closing のまま (close_unknown にしない)
    row = orders.get(env.conn, oid)
    assert row["status"] == "closing"

    env.sched.tick(WED + timedelta(minutes=1))  # quote 復旧 → 再試行で解決
    row2 = orders.get(env.conn, oid)
    assert row2["status"] == "closed"


# --- codex C-I2: データ経路の想定外例外で資金保護に到達しない -------------

LEAKY = "boom https://bridge.internal:8812/orders?apikey=SECRET_KEY_123"


def _assert_no_url(text: str) -> None:
    assert "bridge.internal" not in text
    assert "/orders" not in text
    assert "SECRET_KEY_123" not in text
    assert "apikey" not in text


def _event_names(log_text: str) -> set[str]:
    """activity 行のタブ区切りフィールド (event = index 2) の完全一致集合。

    M-a (fix round 1): 部分一致 (`"foo" in log`) だと接尾辞改名変異
    (例: "foo" -> "foo_v2") が生存する。フィールド単位の完全一致で
    イベント名そのものを固定する。"""
    return {line.split("\t")[2] for line in log_text.splitlines() if line}


def test_mark_to_market_unexpected_exception_does_not_stop_tick(tmp_path):
    """`bars_fn` (= latest_1m_bar) は DataUnhealthy を握って None を返すが、
    `broker.equity()` / `spec_fn` / sqlite3.Error 等の**想定外例外**は
    `_mark_to_market` を貫通し、tick 全体 (= SL/TP 監視) を殺していた。
    snapshot は記録しないが tick は継続すること。"""
    import sqlite3

    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))       # fill -> OPEN
    assert orders.get(env.conn, oid)["status"] == "open"

    def boom():
        raise sqlite3.OperationalError(LEAKY)

    env.executor.broker.equity = boom
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=2),
                             147.60, 147.65, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"                 # 資金保護は止めない
    assert row["close_reason"] == "sl"
    assert env.trade_calls == 1                      # tick は最後まで走った
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "mark_to_market_error" in log
    _assert_no_url(log)                              # C-I3: URL を載せない


def test_snapshot_out_of_order_still_skips_whole_tick(tmp_path):
    """確立済みの裁定 (レビュー修正 3) は変えない — `record_snapshot` の
    ValueError は「時計異常」であり、バー鮮度判定ごと信用できないので
    tick 全体を安全側にスキップする。C-I2 の広い except がこれを
    飲み込んで tick 継続に変わっていないことを固定する。"""
    env = Env(tmp_path)
    env.sched.tick(WED - timedelta(minutes=1))  # 基準 snapshot より過去
    assert env.trade_calls == 0                 # tick 全体がスキップされる
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "snapshot_out_of_order" in log
    assert "mark_to_market_error" not in log    # 広い except の側ではない
    assert "ValueError" in log                  # safe_error_text 経由 (型名付き)


def test_exit_monitoring_continues_when_one_orders_bar_lookup_raises(tmp_path):
    """`_process_exits` は注文単位で例外を隔離する。

    1 注文分の `bars_fn` 例外が、残りの注文の SL/TP 監視を殺してはならない。
    末尾の `_processed_bar_ts` マーキング (settings.pairs 走査) も同じ
    `bars_fn` を呼ぶので、そこで例外が漏れると tick の残り
    (on_trade_mission) まで飛ぶ — こちらも隔離する。
    """
    env = Env(tmp_path)
    # A (先に走査される) = USDJPY: bars_fn が例外を投げるペア
    a = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)
    # B = EURUSD: 正常なバーで SL 到達
    b = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120)
    bars = {"EURUSD": Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                          1.0950, 1.0955, 1.0900, 1.0905, 100)}

    def flaky_bars(pair):
        if pair == "USDJPY":
            raise RuntimeError(LEAKY)
        return bars.get(pair)

    env.sched.bars_fn = flaky_bars
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, a)["status"] == "open"   # A はスキップされるだけ
    row_b = orders.get(env.conn, b)
    assert row_b["status"] == "closed"                   # B の保護は生きている
    assert row_b["close_reason"] == "sl"
    assert env.trade_calls == 1        # マーキング走査でも tick は死なない
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    _assert_no_url(log)


def test_limit_fill_bar_exception_does_not_stop_exit_monitoring(tmp_path):
    """`_process_limit_fills` は `_process_exits` の**前**に走るので、
    そこで例外が漏れると資金保護に到達しない。約定処理も注文単位で隔離する。"""
    env = Env(tmp_path)
    pending = env.place_limit(price=148.20)          # USDJPY (bars_fn が例外)
    b = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120)
    bars = {"EURUSD": Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                          1.0950, 1.0955, 1.0900, 1.0905, 100)}

    def flaky_bars(pair):
        if pair == "USDJPY":
            raise RuntimeError(LEAKY)
        return bars.get(pair)

    env.sched.bars_fn = flaky_bars
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, pending)["status"] == "pending_fill"
    row_b = orders.get(env.conn, b)
    assert row_b["status"] == "closed"
    assert row_b["close_reason"] == "sl"
    assert env.trade_calls == 1


# --- codex C-I3: scheduler が例外を safe_error_text を通さず記録していた ---

def test_reconcile_error_activity_has_no_url(tmp_path):
    env = Env(tmp_path)
    orders.insert(env.conn, pair="USDJPY", direction="long",
                  entry_type="limit", horizon="day", status="cancel_unknown",
                  now=WED, quantity=0.1, requested_price=148.2,
                  stop_loss=147.8)

    def boom(row):
        raise RuntimeError(LEAKY)

    env.executor.broker.reconcile = boom
    env.sched.tick(WED)
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "reconcile_error" in log
    assert "RuntimeError" in log     # 型名は残す (診断)
    _assert_no_url(log)


def test_close_retry_and_day_close_activity_have_no_url(tmp_path):
    """`close_retry_deferred` (quote_fn 例外) / `close_unknown`
    (broker.close 例外) / `day_close_deferred` (quote_fn 例外) の 3 箇所。"""
    def bad_quote(pair):
        raise RuntimeError(LEAKY)

    env = Env(tmp_path, quote_fn=bad_quote)
    orders.insert(env.conn, pair="USDJPY", direction="long",
                  entry_type="limit", horizon="day", status="closing",
                  now=WED, quantity=0.1, requested_price=148.2,
                  stop_loss=147.8, avg_fill_price=148.2)
    env.sched.tick(WED)
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "close_retry_deferred" in log
    _assert_no_url(log)

    # broker.close 例外 (close_unknown)
    env2 = Env(tmp_path / "b")
    orders.insert(env2.conn, pair="USDJPY", direction="long",
                  entry_type="limit", horizon="day", status="closing",
                  now=WED, quantity=0.1, requested_price=148.2,
                  stop_loss=147.8, avg_fill_price=148.2)

    def bad_close(row, price, reason):
        raise RuntimeError(LEAKY)

    env2.executor.broker.close = bad_close
    env2.sched.tick(WED)
    log2 = (env2.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "close_unknown" in log2
    _assert_no_url(log2)

    # day 強制決済の quote 障害 (day_close_deferred)
    env3 = Env(tmp_path / "c")
    oid = env3.place_limit()
    env3.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                              148.25, 100)
    env3.sched.tick(WED + timedelta(minutes=1))     # fill -> OPEN (day)
    env3.executor.quote_fn = bad_quote
    env3.sched.tick(WED.replace(hour=20, minute=57))
    assert orders.get(env3.conn, oid)["status"] == "open"
    log3 = (env3.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "day_close_deferred" in log3
    _assert_no_url(log3)


# --- codex C-M5: tick() の単一呼び出し契約を明文化 ------------------------

def test_tick_docstring_states_single_caller_contract():
    """再入ガードを持たないことは**契約**なので、docstring から消えたら
    落ちるようにしておく (ロックはプラン 5 の service 側 core_lock で持つ)。"""
    doc = Scheduler.tick.__doc__ or ""
    assert "再入ガード" in doc
    assert "単一" in doc


def test_reconcile_pending_activity_has_no_url(tmp_path):
    """`br.message` は **broker が返す外部由来のテキスト**であり、
    `reconcile_pending` は それを `!r` でそのまま activity に書いている。
    Phase 3 の MT5 bridge がエラー本文に自分のエンドポイントを載せれば
    そこから URL が漏れる (C-I3 が挙げた 5 箇所には入っていない経路)。"""
    from agentic_fx.core.contracts import BrokerResult

    env = Env(tmp_path)
    orders.insert(env.conn, pair="USDJPY", direction="long",
                  entry_type="limit", horizon="day", status="cancel_unknown",
                  now=WED, quantity=0.1, requested_price=148.2,
                  stop_loss=147.8)
    env.executor.broker.reconcile = lambda row: BrokerResult(
        status="error", message=f"bridge said: {LEAKY}")
    env.sched.tick(WED)
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "reconcile_pending" in log
    assert "bridge said" in log      # 非 URL 部分の情報は残す
    _assert_no_url(log)


def test_filled_id_is_recorded_before_the_same_bar_exit_check_can_raise(tmp_path):
    """C-I2 の注文単位 try が、レビュー修正 1 (同一バー TP の誤確定) を
    復活させないこと。

    `_process_limit_fills` は約定させた注文を `filled_ids` に入れてから
    同一バーの SL/TP を `entry_same_bar=True` (順序判定不能なので TP は
    抑制) で判定する。この判定が例外を投げたとき、`filled_ids.add` が
    **後**に置かれていると、その注文は `_process_exits` で
    `entry_same_bar=False` として再評価され、本来確定できないはずの TP が
    確定してしまう。try/except の導入で新しく開いた窓なので直接固定する。
    """
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    calls = []
    orig = env.sched._check_one_exit

    def spy(row, bar, *, entry_same_bar):
        calls.append((row["id"], entry_same_bar))
        if entry_same_bar:
            raise RuntimeError(LEAKY)      # 同一バー判定の途中で失敗
        return orig(row, bar, entry_same_bar=entry_same_bar)

    env.sched._check_one_exit = spy
    # 指値 (148.20) に到達し、同一バーで TP (149.00) も超えるバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 149.10, 148.15, 149.00, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert calls == [(oid, True)]      # entry_same_bar=False で再評価しない
    row = orders.get(env.conn, oid)
    assert row["status"] == "open"     # TP は確定させない
    assert row["close_reason"] is None
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "limit_fill_error" in log   # 失敗は無音にしない
    _assert_no_url(log)


# --- 再レビュー N1: filled_ids.add は activity.write より前 -----------------

def test_activity_write_failure_does_not_reopen_same_bar_tp_window(tmp_path):
    """activity.write ("limit_filled") が I/O 障害で例外を投げても、
    同一バー TP 誤確定 (レビュー修正 1 で塞いだ欠陥) が復活しないこと。

    70167fb は filled_ids.add を _check_one_exit の前に置いたが
    activity.write の後のままで、write が OSError (ENOSPC 等) になると
    「DB は OPEN なのに filled_ids に無い」注文が生まれ、_process_exits が
    entry_same_bar=False で再評価して TP を誤確定させていた
    (再レビュー N1、実測で closed/tp を確認)。add を OPEN 遷移直後へ移動。
    """
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    orig = env.sched.activity.write

    def flaky(cat, event, msg, **kw):
        if event == "limit_filled":
            raise OSError("disk full")
        return orig(cat, event, msg, **kw)

    env.sched.activity.write = flaky
    # 148.20 で約定し、同一バーで TP 149.00 にも到達するバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 149.10, 148.15, 149.00, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    row = orders.get(env.conn, oid)
    # 保守則は「同一バーの SL/TP は entry_same_bar=True で判定」であり、
    # activity の障害がその判定条件を変えてはならない
    assert row["status"] == "open", "同一バー TP が誤確定した"
    assert row["close_reason"] is None


# --- 再レビュー N2: 隔離ハンドラ内の activity.write が資金保護を殺す -----

def test_exit_check_activity_write_failure_does_not_stop_remaining_orders(
        tmp_path, caplog):
    """N2 第二層のピン: `_process_exits` の per-order 隔離ハンドラが
    `exit_check_error` を書こうとした ``activity.write`` 自体が (契約を
    破る double で) 例外を投げても、後続の OPEN 注文の SL/TP 監視
    (資金保護の最重要走査) を止めてはならず、tick 自体も完走すること。

    `ActivityLog.write` 本体は一次修正で fail-soft になったが、この double
    はそれを迂回して直接例外を投げるので、一次修正だけでは検出できない
    — 第二層 (呼び出し site 側のさらなる try) をピンする。第二層の
    ``safe_error_text(write_err)`` 経由も、write 例外のメッセージに
    ``LEAKY`` (URL/秘密) を仕込んで確認する。"""
    env = Env(tmp_path)
    # A (先に走査される) = USDJPY: bars_fn が例外を投げる → 隔離ハンドラへ
    a = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)
    # B = EURUSD: 正常なバーで SL 到達
    b = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120)
    bars = {"EURUSD": Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                          1.0950, 1.0955, 1.0900, 1.0905, 100)}

    def flaky_bars(pair):
        if pair == "USDJPY":
            raise RuntimeError("bar lookup down")
        return bars.get(pair)

    orig_write = env.sched.activity.write

    def flaky_write(cat, event, msg, **kw):
        if event == "exit_check_error":
            raise OSError(LEAKY)
        return orig_write(cat, event, msg, **kw)

    env.sched.bars_fn = flaky_bars
    env.sched.activity.write = flaky_write
    with caplog.at_level(logging.WARNING, logger="agentic_fx.scheduler"):
        env.sched.tick(WED + timedelta(minutes=1))
    row_a = orders.get(env.conn, a)
    row_b = orders.get(env.conn, b)
    assert row_a["status"] == "open"     # A は隔離されてスキップされるだけ
    assert row_b["status"] == "closed"   # B の保護は生きている (第二層で保証)
    assert row_b["close_reason"] == "sl"
    assert env.trade_calls == 1          # tick は _process_exits の後まで完走
    _assert_no_url(caplog.text)          # 第二層の safe_error_text(write_err)
    # M-b: 第二層の except 本体が `pass` に変異しても上のアサーションは
    # 全部通ってしまう (B の保護と tick 完走は第二層以前の一次修正だけで
    # 説明できるため)。技術ログ warning が実際に出ることを直接ピンする。
    assert "activity write failed (exit_check_error)" in caplog.text


# --- N3: _cancel_all_pending / _expire_limits の無保護ループ --------------

def test_cancel_all_pending_continues_after_one_cancel_order_raises(tmp_path):
    """N3: equity<=0 (債務超過 = SL/TP が最も要る状態) で呼ばれる
    `_cancel_all_pending` の 1 件目の cancel_order が例外を投げても、
    2 件目の取消と、同一 tick 内の `_process_exits` (資金保護) を
    止めてはならない。例外メッセージは `safe_error_text` を通した形で
    activity に残ること (`LEAKY` で URL/秘密が漏れないことも確認)。"""
    env = Env(tmp_path)
    p1 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.2, stop_loss=147.8)
    p2 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.1, stop_loss=147.7)
    o = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)

    orig_cancel = env.executor.cancel_order

    def flaky_cancel(row, reason):
        if row["id"] == p1:
            raise RuntimeError(LEAKY)
        return orig_cancel(row, reason=reason)

    env.executor.cancel_order = flaky_cancel
    env.executor.broker.equity = lambda: (0.0, 0.0)
    # SL (147.80) を明確に下回るバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    assert orders.get(env.conn, p2)["status"] == "cancelled"  # 2 件目は取消
    row_o = orders.get(env.conn, o)
    assert row_o["status"] == "closed"                        # SL 監視は継続
    assert row_o["close_reason"] == "sl"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "cancel_all_pending_error" in _event_names(log)
    _assert_no_url(log)


def test_expire_limits_continues_after_one_broker_cancel_raises(tmp_path):
    """修正 3: 期限切れ limit の `broker.cancel` 1 件が例外を投げても、
    2 件目の expire 処理と tick 全体は継続すること。例外メッセージは
    `safe_error_text` を通した形で activity に残ること (`LEAKY` で
    URL/秘密が漏れないことも確認)。"""
    expired = (WED - timedelta(minutes=1)).isoformat()
    env = Env(tmp_path)
    e1 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.2, stop_loss=147.8,
                       expires_at=expired)
    e2 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.1, stop_loss=147.7,
                       expires_at=expired)

    orig_cancel = env.executor.broker.cancel

    def flaky_cancel(row):
        if row["id"] == e1:
            raise RuntimeError(LEAKY)
        return orig_cancel(row)

    env.executor.broker.cancel = flaky_cancel
    env.sched.tick(WED)
    assert orders.get(env.conn, e2)["status"] == "expired"    # 2 件目は解決
    assert env.trade_calls == 1                                # tick は完走
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    events = _event_names(log)
    assert "limit_expired" in events
    assert "limit_expire_error" in events
    _assert_no_url(log)


# --- カバレッジ穴 (修正 4): テスト追加のみ、実装変更なし ------------------

def test_record_snapshot_non_value_error_does_not_stop_tick(tmp_path):
    """R10/R11: `record_snapshot` の非 ValueError 分岐 (except Exception 節、
    scheduler.py の record_snapshot 呼び出し直後) が丸ごと削除されても、
    ValueError しか投げないテストでは全緑になり検出できない。
    `sqlite3.OperationalError` で直接ピンする — ①tick は継続 (資金保護実行)
    ②`mark_to_market_error` が activity に載る、の両方を確認する。"""
    import sqlite3

    from agentic_fx.core import scheduler as scheduler_mod

    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))       # fill -> OPEN
    assert orders.get(env.conn, oid)["status"] == "open"

    def boom(conn, *, now, balance, equity):
        raise sqlite3.OperationalError("disk I/O error")

    orig = scheduler_mod.accounting.record_snapshot
    scheduler_mod.accounting.record_snapshot = boom
    try:
        env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=2),
                                 147.60, 147.65, 147.50, 147.55, 100)
        env.sched.tick(WED + timedelta(minutes=2))
    finally:
        scheduler_mod.accounting.record_snapshot = orig

    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"          # 資金保護 (SL) は止まらない
    assert row["close_reason"] == "sl"
    assert env.trade_calls == 1                # tick は最後まで走った
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "mark_to_market_error" in _event_names(log)


def test_news_hook_non_runtime_error_does_not_stop_tick(tmp_path):
    """R2/R3: `_run_data_hook` の隔離 `except Exception` を
    `except RuntimeError` に狭める変異は、RuntimeError しか投げない既存
    テスト (`_boom`) では検出できない。OSError で直接ピンする。"""
    def news_boom():
        raise OSError("news io down")

    env = Env(tmp_path, news_fn=news_boom)
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert env.news_calls == 1
    assert orders.get(env.conn, oid)["status"] == "open"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "news_cycle_error" in _event_names(log)


def test_limit_fill_non_runtime_error_isolated_per_order(tmp_path):
    """R2/R3: `_process_limit_fills` の隔離 `except Exception` を
    `except RuntimeError` に狭める変異は、既存テスト (RuntimeError) では
    検出できない。ValueError で直接ピンする。"""
    env = Env(tmp_path)
    pending = env.place_limit(price=148.20)          # USDJPY (bars_fn が例外)
    b = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120)
    bars = {"EURUSD": Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                          1.0950, 1.0955, 1.0900, 1.0905, 100)}

    def flaky_bars(pair):
        if pair == "USDJPY":
            raise ValueError("bad bar")
        return bars.get(pair)

    env.sched.bars_fn = flaky_bars
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, pending)["status"] == "pending_fill"
    row_b = orders.get(env.conn, b)
    assert row_b["status"] == "closed"
    assert row_b["close_reason"] == "sl"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "limit_fill_error" in _event_names(log)


def test_exit_monitoring_non_runtime_error_isolated_per_order(tmp_path):
    """R2/R3: `_process_exits` の隔離 `except Exception` を
    `except RuntimeError` に狭める変異は、既存テスト (RuntimeError) では
    検出できない。OSError で直接ピンする。"""
    env = Env(tmp_path)
    a = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)
    b = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120)
    bars = {"EURUSD": Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                          1.0950, 1.0955, 1.0900, 1.0905, 100)}

    def flaky_bars(pair):
        if pair == "USDJPY":
            raise OSError("disk down")
        return bars.get(pair)

    env.sched.bars_fn = flaky_bars
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, a)["status"] == "open"
    row_b = orders.get(env.conn, b)
    assert row_b["status"] == "closed"
    assert row_b["close_reason"] == "sl"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "exit_check_error" in _event_names(log)


def test_account_unknown_cancel_pending_activity_event_name(tmp_path):
    """R16: account 不明時の `_cancel_all_pending` イベント名
    (`account_unknown_cancel_pending`) を厳密にピンする。既存テストは
    close_reason の `account_unknown` (部分一致) しか見ておらず、イベント名
    そのものが変わっても検出できなかった。"""
    env = Env(tmp_path)
    orders.insert(env.conn, pair="EURUSD", direction="long",
                 entry_type="market", horizon="swing", status="open",
                 now=WED, quantity=0.1, avg_fill_price=1.1000,
                 stop_loss=1.0960, take_profit=1.1120)
    env.place_limit()  # USDJPY entry=148.20
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=11),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=11))
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "account_unknown_cancel_pending" in _event_names(log)


# --- fix round 1 (独立レビュー fix required) ------------------------------

# C1: _maintain_reservations の while True ループの無保護 cancel_order

def test_maintain_reservations_cancel_order_exception_falls_through_to_exits(
        tmp_path):
    """C1 (Critical): `_maintain_reservations` の `while True` ループは
    予約トリミングの進行 (pending が減ること) を `cancel_order` の成功
    (CANCELLING への遷移) に依存している。ここに try/except → continue を
    当てると、cancel_order が同じ行で毎回 raise する限り同じ行を掴み
    続けて無限ループ (tick ハング) になる (レビュアー実測)。

    正しい修正は except → **return** — この tick の予約トリミングは
    諦め、後続の `_process_exits` (資金保護) に処理を渡す。

    レバレッジ超過の予約 1 件 + SL 到達バーの OPEN 1 件で、
    ①tick が完走する (ハングしない) ②OPEN の SL が同一 tick で
    執行されることを確認する。"""
    env = Env(tmp_path)
    # 予約は別ペア (EURUSD) に直接挿入する。同じ USDJPY にすると、下の
    # OPEN の SL (147.80) を狙ったバーがそのまま予約の entry も割り込み、
    # 同一バーで予約自体が約定してしまい ("予約が生き残っているか" を
    # 検証できなくなる)。gate の limit deviation 制約 (spot ±0.5%) の
    # せいで entry を離すことでも回避できないため、pair を分けて bars_fn
    # がバーを持たない (= 約定判定の対象にならない) ようにする。数値は
    # test_reservation_maintenance_cancels_limit が実測した既定値
    # (quantity 0.12, entry 148.2, stop 147.8) をそのまま複製し、同じ
    # レバレッジ超過 (equity 300,000 → 総リスク上限 4,500 < 予約リスク
    # ≈4,920) を再現する。
    pending = orders.insert(env.conn, pair="EURUSD", direction="long",
                            entry_type="limit", horizon="day",
                            status="pending_fill", now=WED, quantity=0.12,
                            requested_price=148.2, stop_loss=147.8,
                            take_profit=149.0)
    open_id = orders.insert(env.conn, pair="USDJPY", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=148.20, stop_loss=147.80,
                            take_profit=149.00)

    def flaky_cancel(row, reason):
        raise OSError(LEAKY)

    env.executor.cancel_order = flaky_cancel
    env.executor.broker.equity = lambda: (300_000.0, 300_000.0)
    # SL (147.80) を明確に下回るバー (EURUSD のバーは与えないので予約は
    # 約定判定の対象にならない)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # ハングしないこと自体が検証

    row_pending = orders.get(env.conn, pending)
    assert row_pending["status"] == "pending_fill"  # トリミングは諦められた
    row_open = orders.get(env.conn, open_id)
    assert row_open["status"] == "closed"            # SL 監視は継続
    assert row_open["close_reason"] == "sl"
    assert env.trade_calls == 1                      # tick は完走
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    # advisor 指摘: RuntimeError の非漏洩メッセージだけだと
    # safe_error_text 不使用 (str(e)) 変異や except RuntimeError への
    # 狭小化変異、イベント名の接尾辞改名変異のいずれも検出できない。
    # OSError(LEAKY) + イベント名の完全一致で 3 変異まとめて塞ぐ。
    assert "reservation_trim_error" in _event_names(log)
    _assert_no_url(log)


# I1: _expire_limits の期限判定 (datetime.fromisoformat) 自体が隔離の外

def test_expire_limits_invalid_expires_at_isolated_per_order(tmp_path):
    """I1 (Important): `datetime.fromisoformat(row["expires_at"])` が隔離
    try の外にあると、不正な `expires_at` (ISO でない文字列等) を持つ行が
    1 件あるだけで**毎 tick** 同じ行で例外を吐き、資金保護が恒久停止する
    (レビュアー実測)。期限判定を隔離の内側に入れ、当該行だけスキップする
    ことで、SL 監視は同一 tick で執行され、2 tick 目も死なないこと。"""
    env = Env(tmp_path)
    orders.insert(env.conn, pair="USDJPY", direction="long",
                 entry_type="limit", horizon="day", status="pending_fill",
                 now=WED, quantity=0.1, requested_price=148.2,
                 stop_loss=147.8, expires_at="not-a-real-timestamp")
    open_id = orders.insert(env.conn, pair="USDJPY", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=148.20, stop_loss=147.80,
                            take_profit=149.00)
    # 2 tick 目の資金保護も生きていることを直接示すため、別ペアの OPEN を
    # もう 1 件用意し、2 tick 目のバーで SL に到達させる。
    open_id2 = orders.insert(env.conn, pair="EURUSD", direction="long",
                             entry_type="market", horizon="swing",
                             status="open", now=WED, quantity=0.1,
                             avg_fill_price=1.1000, stop_loss=1.0960,
                             take_profit=1.1120)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    row_open = orders.get(env.conn, open_id)
    assert row_open["status"] == "closed"
    assert row_open["close_reason"] == "sl"
    assert env.trade_calls == 1
    # 2 tick 目 (同じ不正行が再び走査される) でも死なず、別ペアの SL 監視
    # (資金保護) が引き続き働くこと。
    env.bars["EURUSD"] = Bar("EURUSD", "1m", WED + timedelta(minutes=2),
                             1.0950, 1.0955, 1.0900, 1.0905, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    row_open2 = orders.get(env.conn, open_id2)
    assert row_open2["status"] == "closed"
    assert row_open2["close_reason"] == "sl"


# I2: _expire_limits の except 内の文言/状態遷移

def test_expire_limits_broker_cancel_exception_transitions_to_cancel_unknown(
        tmp_path):
    """I2 (Important): 以前の except 内文言「— 次 tick 再試行」は事実に
    反していた — `broker.cancel` 例外後の行は `CANCELLING` に留まり、
    `_expire_limits` の `PENDING_FILL` 走査にも `_resolve_unknowns` の
    走査集合にも入らず、誰も再試行しない (`_UNKNOWN` に含まれず gate も
    止まらない一方、`_EXPOSURE` には算入されリスク枠を恒久占有する —
    レビュアー実測)。

    修正: 例外源が CANCELLING 遷移そのものでない限り (= 現在状態が
    CANCEL_UNKNOWN へ遷移可能な限り) CANCEL_UNKNOWN へ遷移させ、
    `_resolve_unknowns` の走査に乗せる。paper broker の reconcile は
    常に not_found を返すため、次 tick で実際に `cancelled` まで解決
    されることを確認する。"""
    expired = (WED - timedelta(minutes=1)).isoformat()
    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="pending_fill", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        expires_at=expired)

    def bad_broker_cancel(row):
        raise RuntimeError("broker cancel timeout")

    env.executor.broker.cancel = bad_broker_cancel
    env.sched.tick(WED)
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancel_unknown"
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "次 tick 再試行" not in log      # 事実に反する文言は消えている
    assert "cancel_unknown" in log

    env.sched.tick(WED + timedelta(minutes=1))  # _resolve_unknowns が先に走る
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_expire_limits_broker_rejected_write_failure_does_not_mark_cancel_unknown(
        tmp_path):
    """I2 の再検証 (advisor 指摘): `broker.cancel` が `"rejected"`
    (= 既に約定済み・保護されている) を返した**後**、その結果を DB に
    落とす `transitions.transition(..., PROTECTION_PENDING, ...)` が
    失敗した場合、`CANCEL_UNKNOWN` へ遷移させてはならない。
    `CANCEL_UNKNOWN` は `_resolve_unknowns` 経由で reconcile され、
    paper broker は常に not_found を返すため `cancelled` に落ちる —
    broker が「もう埋まっている」と教えてくれた注文を、こちら側の
    書き込み失敗のせいで黙って取消済みにしてしまうのは資金保護上の
    ハザードになる (Phase 1 の PaperBroker.cancel は常に "ok" しか
    返さないため未到達だが、Phase 3 の MT5 で "rejected" が現実になる)。
    broker が答えを返した**後**の失敗は CANCEL_UNKNOWN 化の対象外とし、
    CANCELLING のまま (次 tick は PENDING_FILL の走査対象からは外れる
    ため、手動確認待ちになる — これは「broker 未接触」のケースより
    悪化させない、という保守側の選択)。"""
    from agentic_fx.core import scheduler as scheduler_mod
    from agentic_fx.core.contracts import BrokerResult

    expired = (WED - timedelta(minutes=1)).isoformat()
    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="pending_fill", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        expires_at=expired)

    env.executor.broker.cancel = lambda row: BrokerResult(status="rejected")

    orig_transition = scheduler_mod.transitions.transition

    def flaky_transition(conn, order_id, to, now, **fields):
        if order_id == oid and to.value == "protection_pending":
            raise RuntimeError("db write failed")
        return orig_transition(conn, order_id, to, now, **fields)

    scheduler_mod.transitions.transition = flaky_transition
    try:
        env.sched.tick(WED)
    finally:
        scheduler_mod.transitions.transition = orig_transition

    row = orders.get(env.conn, oid)
    assert row["status"] != "cancel_unknown"  # broker 応答後の書き込み
    # 失敗を CANCEL_UNKNOWN 化 (→ 誤って cancelled に落ちる) してはならない
    assert row["status"] == "cancelling"
    assert env.trade_calls == 1  # tick は完走


def test_expire_limits_cancelling_transition_exception_stays_isolated(
        tmp_path):
    """I2 の「遷移の罠」: 例外源が `CANCELLING` への遷移そのものだった
    場合、行はまだ `PENDING_FILL` のままで `ALLOWED[PENDING_FILL]` に
    `CANCEL_UNKNOWN` は無く、無条件に遷移を試みると `IllegalTransition`
    が隔離ハンドラから漏れる。現在状態を読み直して分岐する (or 遷移
    呼び出し自体を独自の try で包む) ことで、この罠を踏まず隔離を
    破らないこと。"""
    from agentic_fx.core import scheduler as scheduler_mod

    expired = (WED - timedelta(minutes=1)).isoformat()
    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="pending_fill", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        expires_at=expired)

    orig_transition = scheduler_mod.transitions.transition

    def flaky_transition(conn, order_id, to, now, **fields):
        if order_id == oid and to.value == "cancelling":
            raise RuntimeError("db write failed")
        return orig_transition(conn, order_id, to, now, **fields)

    scheduler_mod.transitions.transition = flaky_transition
    try:
        env.sched.tick(WED)   # IllegalTransition が隔離ハンドラから漏れない
    finally:
        scheduler_mod.transitions.transition = orig_transition

    row = orders.get(env.conn, oid)
    assert row["status"] == "pending_fill"   # CANCELLING 自体が失敗 → 未変化
    assert env.trade_calls == 1              # tick は完走


# I4: 本ラウンドで新設した隔離 site の except RuntimeError 変異が生存

def test_cancel_all_pending_non_runtime_error_isolated_per_order(tmp_path):
    """I4: `_cancel_all_pending` の隔離 `except Exception` を
    `except RuntimeError` に狭める変異は、RuntimeError しか投げない
    既存テストでは検出できない。OSError で直接ピンする。"""
    env = Env(tmp_path)
    p1 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.2, stop_loss=147.8)
    p2 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.1, stop_loss=147.7)
    o = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)

    orig_cancel = env.executor.cancel_order

    def flaky_cancel(row, reason):
        if row["id"] == p1:
            raise OSError("cancel io down")
        return orig_cancel(row, reason=reason)

    env.executor.cancel_order = flaky_cancel
    env.executor.broker.equity = lambda: (0.0, 0.0)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    assert orders.get(env.conn, p2)["status"] == "cancelled"
    row_o = orders.get(env.conn, o)
    assert row_o["status"] == "closed"
    assert row_o["close_reason"] == "sl"


def test_expire_limits_non_runtime_error_isolated_per_order(tmp_path):
    """I4: `_expire_limits` の隔離 `except Exception` を `except
    RuntimeError` に狭める変異は、既存テストでは検出できない。ValueError
    で直接ピンする。"""
    expired = (WED - timedelta(minutes=1)).isoformat()
    env = Env(tmp_path)
    e1 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.2, stop_loss=147.8,
                       expires_at=expired)
    e2 = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="limit", horizon="day",
                       status="pending_fill", now=WED, quantity=0.1,
                       requested_price=148.1, stop_loss=147.7,
                       expires_at=expired)

    orig_cancel = env.executor.broker.cancel

    def flaky_cancel(row):
        if row["id"] == e1:
            raise ValueError("broker cancel bad state")
        return orig_cancel(row)

    env.executor.broker.cancel = flaky_cancel
    env.sched.tick(WED)
    assert orders.get(env.conn, e2)["status"] == "expired"
    assert env.trade_calls == 1


# --- fix round 2 (独立レビュー再判定 — fix diff 自体が開けた新規欠陥 2 件) --

def test_expire_limits_recovery_orders_get_failure_does_not_stop_tick(
        tmp_path, caplog):
    """新規 Critical: `_expire_limits` の except ハンドラ内の復旧処理
    (`orders.get` / `S(current["status"])`) が無保護だった。内側 try
    (`transitions.transition` のみ) の外にあり、broker.cancel が DB 障害の
    連鎖で例外を投げる状況では、**同じ conn** を使うこの `orders.get` も
    同じ理由で落ちる。その例外は except 節から漏れ、`_expire_limits` に
    外側 try が無いため tick を貫通し、後続の `_process_exits`
    (SL 執行) が丸ごと死ぬ (レビュアー実測: sqlite3.OperationalError で
    SL 未執行)。

    再現はレビュアーが実測に使った probe を書き直したもの。broker.cancel
    が 1 回目の期限切れ注文で raise し、その直後に呼ばれる復旧処理内の
    `orders.get` も 1 回だけ raise するよう仕込む。"""
    import sqlite3

    from agentic_fx.core import scheduler as scheduler_mod

    expired = (WED - timedelta(minutes=1)).isoformat()
    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="pending_fill", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        expires_at=expired)
    open_id = orders.insert(env.conn, pair="EURUSD", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=1.1000, stop_loss=1.0960,
                            take_profit=1.1120)

    state = {"armed": False}

    def flaky_broker_cancel(row):
        state["armed"] = True
        raise RuntimeError("broker down (db fault cascade)")

    env.executor.broker.cancel = flaky_broker_cancel

    orig_get = scheduler_mod.orders.get

    def flaky_get(conn, order_id):
        if state["armed"]:
            state["armed"] = False       # ハンドラの 1 回だけ落とす
            # LEAKY: 修正後の内側 except が safe_error_text を通すことも
            # 同時にピンする (str(e) 変異を殺す)。
            raise sqlite3.OperationalError("disk I/O error " + LEAKY)
        return orig_get(conn, order_id)

    scheduler_mod.orders.get = flaky_get
    # EURUSD の SL (1.0960) を明確に下回るバー
    env.bars["EURUSD"] = Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                             1.0950, 1.0955, 1.0900, 1.0905, 100)
    try:
        with caplog.at_level(logging.WARNING, logger="agentic_fx.scheduler"):
            env.sched.tick(WED + timedelta(minutes=1))
    finally:
        scheduler_mod.orders.get = orig_get

    row_open = orders.get(env.conn, open_id)
    assert row_open["status"] == "closed", "資金保護 (SL) が止まった"
    assert row_open["close_reason"] == "sl"
    assert env.trade_calls == 1, "tick が完走していない"
    assert orders.get(env.conn, oid) is not None
    _assert_no_url(caplog.text)


def test_maintain_reservations_cancel_order_failure_does_not_allow_same_tick_fill(
        tmp_path):
    """新規 Important: C1 の `return` が予約約定の fail-open 窓を開けた。

    `_maintain_reservations` がリスク超過と判定した予約の取消に失敗して
    `return` しても、tick 側の `fills_allowed` は True のままだったため、
    同一 tick の `_process_limit_fills` が到達バーでその予約を約定させて
    しまっていた (gate は予約約定経路では再実行されない — レビュアー実測:
    a0a014f 時点では約定しなかったのに、C1 の fix 由来でこの回帰が入った)。

    修正: `_maintain_reservations` が中断 (bool False) を呼び出し元へ返し、
    tick がその tick の `fills_allowed` を False にする (account 不明/
    equity<=0 と同じ既存の 2 層構えに揃える)。`_process_exits` は
    `filled_ids=set()` で通常どおり走り続けること (C1 の目的である資金
    保護継続と両立) も確認する。"""
    env = Env(tmp_path)
    oid = env.place_limit()          # USDJPY limit @148.20 (qty 0.12 相当)
    open_id = orders.insert(env.conn, pair="EURUSD", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=1.1000, stop_loss=1.0960,
                            take_profit=1.1120)
    env.executor.broker.equity = lambda: (300_000.0, 300_000.0)  # リスク超過

    def flaky_cancel(row, reason):
        raise OSError("cancel io down")

    env.executor.cancel_order = flaky_cancel
    # entry (148.20) に到達する USDJPY のバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 148.15, 148.25, 100)
    # SL (1.0960) を明確に下回る EURUSD のバー — 資金保護は継続すること
    env.bars["EURUSD"] = Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                             1.0950, 1.0955, 1.0900, 1.0905, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    row = orders.get(env.conn, oid)
    # fix round 3 (レビュアー注記, Minor): `!= "open"` より `==
    # "pending_fill"` の方が締まる (取消も約定もしていない、が正確な期待)。
    assert row["status"] == "pending_fill", (
        "リスク超過で取消対象と判定された予約が同一 tick で約定した "
        f"(status={row['status']})")
    row_open = orders.get(env.conn, open_id)
    assert row_open["status"] == "closed"   # SL/TP 走査は通常どおり動く
    assert row_open["close_reason"] == "sl"
    assert env.trade_calls == 1


# --- fix round 3 (独立レビュー再判定 — 未裁定の既存無保護 site 2 件) -------
#
# 判定基準: 「pre-_process_exits の障害は、_process_exits 自身が同じ障害を
# 生き延びるならば欠陥」— _process_exits は注文単位隔離 (line ~700 台) を
# 持つのでペアローカル障害を吸収するのに、その前に立つ無保護呼び出しで
# tick 全体が死ぬ非対称性が問題。

def test_maintain_reservations_call_spec_fn_failure_does_not_stop_tick(
        tmp_path):
    """site 1 (Critical 相当): `_maintain_reservations` 呼び出し
    (`tick():153`) が無保護。内部の `open_risk_and_notional`
    (`executor.py:41`) は `_EXPOSURE` 全行に `spec_fn(pair)` を回すため、
    1 件の予約が spec を引けないペア (DataUnhealthy 等) だと tick 全体が
    死ぬ (レビュアー実測: GBPJPY の PENDING_FILL 1 件 + spec_fn が GBPJPY
    のみ raise + USDJPY OPEN に SL 割れバー → RuntimeError が tick を
    貫通し USDJPY の SL 未執行)。呼び出しを try/except で包み、例外時は
    `fills_allowed = False` にして (取消失敗時の既存 2 層構えと同じ扱い)、
    後続の `_process_exits` (資金保護) は必ず生かす。2 tick 目も死なない
    ことも確認する。"""
    env = Env(tmp_path)
    gbp = orders.insert(env.conn, pair="GBPJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="pending_fill", now=WED, quantity=0.1,
                        requested_price=190.0, stop_loss=189.0)
    open_id = orders.insert(env.conn, pair="USDJPY", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=148.20, stop_loss=147.80,
                            take_profit=149.00)
    # 2 tick 目の資金保護も生きていることを直接示すため、別ペアの OPEN を
    # もう 1 件用意し、2 tick 目のバーで SL に到達させる。
    open_id2 = orders.insert(env.conn, pair="EURUSD", direction="long",
                             entry_type="market", horizon="swing",
                             status="open", now=WED, quantity=0.1,
                             avg_fill_price=1.1000, stop_loss=1.0960,
                             take_profit=1.1120)

    orig_spec_fn = env.executor.spec_fn

    def flaky_spec_fn(pair):
        # DataUnhealthy (非 RuntimeError): 「except を RuntimeError に
        # 狭める」変異を殺すための実際の想定例外型でもある。
        if pair == "GBPJPY":
            from agentic_fx.datafeed.health import DataUnhealthy
            raise DataUnhealthy(LEAKY)
        return orig_spec_fn(pair)

    env.executor.spec_fn = flaky_spec_fn
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.50, 147.55, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    row_open = orders.get(env.conn, open_id)
    assert row_open["status"] == "closed"          # SL 監視は継続
    assert row_open["close_reason"] == "sl"
    assert env.trade_calls == 1
    row_gbp = orders.get(env.conn, gbp)
    assert row_gbp["status"] == "pending_fill"     # 取消は諦められた
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "maintain_reservations_error" in _event_names(log)
    _assert_no_url(log)

    # 2 tick 目 (同じ GBPJPY 行が再び走査される) でも死なず、別ペアの SL
    # 監視 (資金保護) が引き続き働くこと。
    env.bars["EURUSD"] = Bar("EURUSD", "1m", WED + timedelta(minutes=2),
                             1.0950, 1.0955, 1.0900, 1.0905, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    row_open2 = orders.get(env.conn, open_id2)
    assert row_open2["status"] == "closed"
    assert row_open2["close_reason"] == "sl"


def test_force_close_day_close_order_failure_isolated_per_position(
        tmp_path):
    """site 2 (Critical 相当): `_force_close_day` の `close_order`
    (`tick():155` → `scheduler.py` 内) が無保護 (既存 try は `quote_fn`
    だけを包んでいた)。1 件の day ポジションの `close_order` 例外
    (spec_fn の DataUnhealthy 等) が `_force_close_day` → tick を貫通し、
    後続の `_process_exits` (SL/TP 監視) を丸ごと止めていた (レビュアー
    実測: EURUSD の day ポジションの close_order だけ raise →
    RuntimeError が tick を貫通、USDJPY の SL 未執行)。ポジション単位で
    隔離し、`datetime.fromisoformat(anchor)` (I1 と同型) も保護の内側へ
    入れる。

    注文状態がどちらに転んでも (OPEN のまま → 次 tick に `_force_close_day`
    自身が再走査 / CLOSING に進んだ → `_resolve_unknowns` の
    `_retry_close` が次 tick 再走査) 必ず再試行されるため、「次 tick
    再試行」の文言は事実に即している。除外処理 (except ハンドラ) は
    `orders.get` 等の復旧処理を追加しない (F1 の教訓: 復旧処理自体が
    同じ故障源で落ちるのを避けるため、そもそも呼ばない)。"""
    env = Env(tmp_path)
    # A: EURUSD の day ポジション — close_order 内の spec_fn が例外を投げる
    a = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="day", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120,
                      filled_at=WED.isoformat())
    # B: USDJPY の swing ポジション — SL 到達 (1 tick 目)
    b = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)
    # C: GBPJPY の swing ポジション — SL 到達 (2 tick 目、資金保護の継続確認用)
    c = orders.insert(env.conn, pair="GBPJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=190.00,
                      stop_loss=189.00, take_profit=192.00)

    orig_spec_fn = env.executor.spec_fn

    def flaky_spec_fn(pair):
        # DataUnhealthy (非 RuntimeError): 「except を RuntimeError に
        # 狭める」変異を殺すための実際の想定例外型でもある。
        if pair == "EURUSD":
            from agentic_fx.datafeed.health import DataUnhealthy
            raise DataUnhealthy(LEAKY)
        return orig_spec_fn(pair)

    env.executor.spec_fn = flaky_spec_fn
    near_close = WED.replace(hour=20, minute=57)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", near_close,
                             147.60, 147.65, 147.50, 147.55, 100)
    env.sched.tick(near_close)

    row_a = orders.get(env.conn, a)
    row_b = orders.get(env.conn, b)
    assert row_a["status"] == "open"     # A は隔離されただけ (close 未実施)
    assert row_b["status"] == "closed"   # B の SL 監視は生きている
    assert row_b["close_reason"] == "sl"
    assert env.trade_calls == 1
    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "day_close_error" in _event_names(log)
    _assert_no_url(log)

    # 2 tick 目 (同じ EURUSD ポジションが再び走査される) でも死なず、
    # 別ペア (GBPJPY) の SL 監視が引き続き働くこと。
    env.bars["GBPJPY"] = Bar("GBPJPY", "1m", near_close + timedelta(minutes=1),
                             189.50, 189.55, 188.50, 188.90, 100)
    env.sched.tick(near_close + timedelta(minutes=1))
    row_c = orders.get(env.conn, c)
    assert row_c["status"] == "closed"
    assert row_c["close_reason"] == "sl"


def test_force_close_day_invalid_filled_at_isolated_per_position(tmp_path):
    """site 2 の I1 相当部分: `datetime.fromisoformat(anchor)`
    (旧 `scheduler.py:606` 付近) が隔離の外にあると、不正な `filled_at`/
    `created_at` を持つ day ポジション 1 件で毎 tick 同じ行が例外を吐き、
    資金保護が恒久停止する。期限判定を隔離の内側に入れ、当該ポジション
    だけスキップすること。"""
    env = Env(tmp_path)
    a = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="day", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0960, take_profit=1.1120,
                      filled_at="not-a-real-timestamp")
    b = orders.insert(env.conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="swing", status="open",
                      now=WED, quantity=0.1, avg_fill_price=148.20,
                      stop_loss=147.80, take_profit=149.00)
    near_close = WED.replace(hour=20, minute=57)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", near_close,
                             147.60, 147.65, 147.50, 147.55, 100)
    env.sched.tick(near_close)

    row_a = orders.get(env.conn, a)
    row_b = orders.get(env.conn, b)
    assert row_a["status"] == "open"     # 不正な filled_at はスキップされるだけ
    assert row_b["status"] == "closed"
    assert row_b["close_reason"] == "sl"
    assert env.trade_calls == 1


# --- fix round 4 (独立レビュー再判定 — fix diff の主張の未検証部分) --------
#
# round 3 の fix diff にコード上の新規欠陥は無かったが、変異テストで
# 2 件が生存していた (レビュアーが実行可能なテストを作成済み — 本節は
# それを移植したもの)。

def test_site1_fills_allowed_false_prevents_reservation_fill_different_pair(
        tmp_path):
    """変異 A の識別テスト (Important): site 1 の except で
    `fills_allowed = False` (`scheduler.py:172` 付近) を削除しても、
    既存の site 1 テスト (`test_maintain_reservations_call_spec_fn_failure_does_not_stop_tick`)
    は raise させるペアと予約のペアが同一 (GBPJPY) かつ GBPJPY にバーを
    与えていないため約定経路に到達せず、この変異を殺せなかった (実測)。

    raise させるペア (USDJPY, `spec_fn` が `DataUnhealthy`) と予約の
    ペア (EURUSD, 到達バーあり) を**別**にすることで、
    `fills_allowed=False` が落ちると「総リスクを再検証できない tick で
    EURUSD の予約が約定してしまう」fail-open (fix round 2 の C1 と同じ
    クラス) を直接ピンする。

    罠 (レビュアー実測): spread = half 0.005 のため、`bar.low + half` が
    指値を上回ると約定しない。SL は 1.0500 を使う — SL を 1.0900 に近い
    値にすると、同一バー内で `entry_same_bar=True` の `check_exit` が
    即座にクローズしてしまい、約定直後の `open` 状態を観測できなくなる
    (指値到達と SL 到達がこの 1 本のバーで同時に起こるため)。"""
    env = Env(tmp_path)
    # USDJPY の OPEN — _EXPOSURE に入るので open_risk_and_notional が
    # spec_fn(USDJPY) を呼び、raise して site 1 の except に落ちる。
    open_id = orders.insert(env.conn, pair="USDJPY", direction="long",
                            entry_type="market", horizon="swing",
                            status="open", now=WED, quantity=0.1,
                            avg_fill_price=148.20, stop_loss=147.80,
                            take_profit=149.00)
    # EURUSD の予約 — 到達バーを与える (USDJPY とは別ペア)。
    res = orders.insert(env.conn, pair="EURUSD", direction="long",
                        entry_type="limit", horizon="day",
                        status="pending_fill", now=WED, quantity=0.1,
                        requested_price=1.1000, stop_loss=1.0500,
                        take_profit=1.1200,
                        expires_at=(WED + timedelta(hours=4)).isoformat())

    orig_spec_fn = env.executor.spec_fn

    def flaky_spec_fn(pair):
        if pair == "USDJPY":
            from agentic_fx.datafeed.health import DataUnhealthy
            raise DataUnhealthy(LEAKY)
        return orig_spec_fn(pair)

    env.executor.spec_fn = flaky_spec_fn
    # spread = assumed_spread_pips(1.0) * SPEC.pip_size(0.01) = 0.01 →
    # half=0.005。long の指値到達は bar.low + 0.005 <= 1.1000 が必要。
    env.bars["EURUSD"] = Bar("EURUSD", "1m", WED + timedelta(minutes=1),
                             1.1050, 1.1055, 1.0900, 1.1000, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    names = _event_names(log)
    # 前提: site 1 の except に到達している (mark_to_market は同じ raise を
    # 握って tick を継続する)。
    assert "maintain_reservations_error" in names, names
    row_res = orders.get(env.conn, res)
    assert row_res["status"] == "pending_fill", (
        "総リスクを再検証できない tick で予約が約定した (fail-open): "
        f"status={row_res['status']}")
    assert orders.get(env.conn, open_id)["status"] == "open"


def test_site2_close_order_failure_does_not_stop_remaining_day_positions(
        tmp_path):
    """変異 B の識別テスト (Minor): site 2 の except 末尾に `return`
    を足しても (ループを打ち切っても)、既存の site 2 テスト
    (`test_force_close_day_close_order_failure_isolated_per_position`)
    は day ポジションが 1 件しか無いため 2 件目への継続が検証されず、
    この変異を殺せなかった (実測)。

    day ポジションを 2 件にし、1 件目 (EURUSD) の `close_order` が
    (内部の `spec_fn` の `DataUnhealthy` で) raise しても、2 件目
    (GBPJPY, 健全) の day 強制決済が同一 tick で実行され続けることを
    直接ピンする (`return` だと 2 件目が rollover を越えて持ち越される)。"""
    env = Env(tmp_path)
    a = orders.insert(env.conn, pair="EURUSD", direction="long",
                      entry_type="market", horizon="day", status="open",
                      now=WED, quantity=0.1, avg_fill_price=1.1000,
                      stop_loss=1.0900, take_profit=1.1200,
                      filled_at=WED.isoformat())
    b = orders.insert(env.conn, pair="GBPJPY", direction="long",
                      entry_type="market", horizon="day", status="open",
                      now=WED, quantity=0.1, avg_fill_price=190.00,
                      stop_loss=189.00, take_profit=192.00,
                      filled_at=WED.isoformat())

    orig_spec_fn = env.executor.spec_fn

    def flaky_spec_fn(pair):
        if pair == "EURUSD":
            from agentic_fx.datafeed.health import DataUnhealthy
            raise DataUnhealthy(LEAKY)
        return orig_spec_fn(pair)

    env.executor.spec_fn = flaky_spec_fn
    near_close = WED.replace(hour=20, minute=57)
    env.sched.tick(near_close)

    log = (env.tmp_path / "a.log").read_text(encoding="utf-8")
    assert "day_close_error" in _event_names(log)
    assert orders.get(env.conn, a)["status"] == "open"   # A は隔離される
    row_b = orders.get(env.conn, b)
    assert row_b["status"] == "closed", (
        "1 件目の close_order 例外で 2 件目の day 強制決済が止まった "
        f"(status={row_b['status']}) — rollover 越えの持ち越し")
    assert row_b["close_reason"] == "day_rollover"

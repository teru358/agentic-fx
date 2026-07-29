import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core import market_hours
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Bar, FixedClock, InstrumentSpec, Mode, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor, has_unresolved_unknown
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
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000)
QUOTE = Quote("USDJPY", 148.49, 148.51, WED, "test")


class Env:
    def __init__(self, tmp_path, quote_fn=None, base=WED, news_fn=None,
                 econ_fn=None):
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
            quote_fn=quote_fn or (lambda p: QUOTE), spec_fn=lambda p: SPEC)
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
    """レビュー修正 6: equity<=0 では notional/equity がゼロ除算になる。"""
    env = Env(tmp_path)
    oid = env.place_limit()
    env.executor.broker.equity = lambda: (0.0, 0.0)
    env.sched.tick(WED + timedelta(minutes=1))  # ZeroDivisionError にならない
    row = orders.get(env.conn, oid)
    assert row["status"] == "pending_fill"  # gate 側の fail closed に委ねる


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

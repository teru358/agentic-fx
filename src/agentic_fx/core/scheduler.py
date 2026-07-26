"""Scheduler — クロック駆動 tick。mark-to-market・reconcile・予約維持・約定・毎時 Mission。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core import accounting, market_hours, transitions
from agentic_fx.core.contracts import Bar, Mode, OrderStatus as S
from agentic_fx.core.executor import Executor, open_risk_and_notional
from agentic_fx.core.paper_fills import check_exit, check_limit_fill
from agentic_fx.store import orders
from agentic_fx.store.state import StateStore

_log = logging.getLogger("agentic_fx.scheduler")
_NEWS_INTERVAL = timedelta(minutes=30)
_DAY_CLOSE_BUFFER = timedelta(minutes=5)
_BAR_FRESHNESS = timedelta(minutes=5)


class Scheduler:
    """クロック駆動 tick。

    クロック契約 (レビュー修正 8): ``tick(now)`` に渡す ``now`` は
    ``Executor.clock.now()`` と **同一の Clock** から供給すること。両者が
    ズレると (例: tick 側だけ実時計、Executor 側だけ別の固定時計)、
    ``expires_at`` 等 Executor 側で計算した時刻と tick の判定基準が食い違い、
    期限切れ判定やクローズ移行判定が意図せずズレる。テストの `Env` フィクス
    チャは両者に同じ基準時刻を渡すことでこれを担保している。
    """

    def __init__(self, *, conn: sqlite3.Connection, executor: Executor,
                 settings: Settings, state_store: StateStore,
                 activity: ActivityLog,
                 bars_fn: Callable[[str], Bar | None],
                 on_trade_mission: Callable[[], None],
                 on_news_cycle: Callable[[], None]) -> None:
        self.conn = conn
        self.executor = executor
        self.settings = settings
        self.state = state_store
        self.activity = activity
        self.bars_fn = bars_fn
        self.on_trade_mission = on_trade_mission
        self.on_news_cycle = on_news_cycle
        self._last_trade: datetime | None = None
        self._last_news: datetime | None = None
        self._was_open: bool | None = None
        self._processed_bar_ts: dict[str, datetime] = {}

    def tick(self, now: datetime) -> None:
        open_now = market_hours.is_market_open(now)
        if not open_now:
            # レビュー修正 4: _was_open はプロセスメモリのみに保持される。
            # サービスがオープン中に落ち、クローズ後に再起動すると最初の
            # tick で _was_open は None (True でも False でもない) になる。
            # 「True か不明(None)」ならクローズ移行処理を実行する
            # (learning モードなら _on_market_close が早期 return するので無害)。
            if self._was_open is not False:
                self._on_market_close(now)
            self._was_open = False
            if self._last_news is None or now - self._last_news >= _NEWS_INTERVAL:
                self._last_news = now
                self.on_news_cycle()
            return
        self._was_open = True

        # レビュー修正 3: record_snapshot が時系列逆行 (NTP 補正等) で
        # ValueError を送出した場合、mark-to-market 自体が信頼できないため、
        # この tick は安全側に全体スキップする。
        if not self._mark_to_market(now):
            return
        self._resolve_unknowns(now)
        self._expire_limits(now)
        self._maintain_reservations(now)
        self._force_close_day(now)
        self._process_fills(now)
        if self._last_trade is None or now - self._last_trade >= timedelta(hours=1):
            self._last_trade = now
            self.on_trade_mission()

    # ---- internal -------------------------------------------------------

    def _fresh_bar(self, pair: str, now: datetime) -> Bar | None:
        """鮮度検証 + 同一バー再処理防止を通ったバーのみ返す。"""
        bar = self.bars_fn(pair)
        if bar is None or now - bar.ts > _BAR_FRESHNESS:
            return None
        if self._processed_bar_ts.get(pair) == bar.ts:
            return None
        return bar

    def _mark_to_market(self, now: datetime) -> bool:
        """時価 snapshot — gate の equity/hwm と kill switch (unrealized 込み) の源泉。

        戻り値は「tick 全体を継続してよいか」(record_snapshot の時系列逆行の
        みFalse)。open ポジションのバーが欠落/陳腐化している場合は **この
        tick の snapshot 記録だけ** を見送る (レビュー修正 7: 古い価格で
        含み損益を計算し kill switch の判定を誤らせないため)。tick 自体は
        継続する (day 強制クローズ等はバー鮮度に依存せず独立して機能する)。
        """
        balance, _ = self.executor.broker.equity()
        unrealized = 0.0
        stale = False
        for row in orders.list_by_status(self.conn, S.OPEN):
            bar = self.bars_fn(row["pair"])
            if bar is None or now - bar.ts > _BAR_FRESHNESS:
                stale = True
                self.activity.write(
                    Category.SYSTEM, "snapshot_stale_bar_skip",
                    f"{row['pair']}: バー欠落/陳腐化 — 時価評価を見送り",
                    ref_id=str(row["id"]))
                continue
            spec = self.executor.spec_fn(row["pair"])
            sign = 1.0 if row["direction"] == "long" else -1.0
            unrealized += (bar.close - row["avg_fill_price"]) \
                * spec.contract_size * (row["quantity"] or 0.0) * sign
        if stale:
            return True  # snapshot は記録しないが tick は継続する
        try:
            accounting.record_snapshot(self.conn, now=now, balance=balance,
                                       equity=balance + unrealized)
        except ValueError as e:
            self.activity.write(Category.SYSTEM, "snapshot_out_of_order",
                                str(e))
            _log.warning("mark-to-market skipped (clock regression?): %s", e)
            return False
        return True

    def _resolve_unknowns(self, now: datetime) -> None:
        resolution = {
            S.SUBMIT_UNKNOWN.value: S.REJECTED,   # 注文なし → 発注されていない
            S.CANCEL_UNKNOWN.value: S.CANCELLED,  # 注文なし → 取消済み
            S.CLOSE_UNKNOWN.value: S.CLOSING,     # 再試行へ
        }
        for row in orders.list_by_status(self.conn, S.SUBMIT_UNKNOWN,
                                         S.CANCEL_UNKNOWN, S.CLOSE_UNKNOWN):
            br = self.executor.broker.reconcile(row)
            if br.status != "ok":
                continue  # まだ不明 — 新規発注停止は gate が継続
            to = resolution[row["status"]]
            transitions.transition(self.conn, row["id"], to, now)
            self.activity.write(Category.TRADE, "unknown_resolved",
                                f"#{row['id']} -> {to.value}",
                                ref_id=str(row["id"]))
        # レビュー修正 2: CLOSING (今 tick 新規に遷移したものも、過去 tick から
        # quote 障害等で滞留しているものも含む) を毎 tick 再走査し、quote が
        # 復旧次第クローズを完結させる。executor._UNKNOWN 側にも CLOSING を
        # 含めているため、解決するまでは gate が新規発注を止め続ける。
        for row in orders.list_by_status(self.conn, S.CLOSING):
            self._retry_close(row, now)

    def _retry_close(self, row: dict, now: datetime) -> None:
        """CLOSING の再試行。quote 障害は次 tick 再試行として握りつぶすが、
        broker.close が成功した後の DB 確定処理 (pnl 計算・遷移) の失敗は
        握りつぶさない (broker 側は既にクローズ済みかもしれず、不整合を
        静かに握りつぶすのは資金保護上危険なため — レビュー修正 2)。"""
        try:
            q = self.executor.quote_fn(row["pair"])
        except Exception as e:  # noqa: BLE001 — quote 障害のみ次 tick 再試行
            self.activity.write(Category.TRADE, "close_retry_deferred",
                                f"{row['pair']}: {e}", ref_id=str(row["id"]))
            _log.warning("close retry deferred (quote) #%s: %s", row["id"], e)
            return
        price = q.bid if row["direction"] == "long" else q.ask
        fresh = orders.get(self.conn, row["id"])
        br2 = self.executor.broker.close(fresh, price, "retry")
        if br2.status == "ok":
            # broker 側は成功済み — 以降の DB 確定処理の失敗は握りつぶさない
            from agentic_fx.core.paper_broker import compute_pnl
            spec = self.executor.spec_fn(row["pair"])
            pnl = compute_pnl(
                fresh, price, contract_size=spec.contract_size,
                commission_per_lot=self.settings.risk.commission_per_lot)
            transitions.transition(
                self.conn, row["id"], S.CLOSED, now,
                close_price=price, realized_pnl=pnl, closed_at=now.isoformat())
        else:
            transitions.transition(self.conn, row["id"], S.CLOSE_UNKNOWN, now)

    def _on_market_close(self, now: datetime) -> None:
        """クローズ移行時: 取引モードは実指値を監視外に残さない (設計書 §5)。"""
        if self.state.load().mode is not Mode.TRADING:
            return
        for row in orders.list_by_status(self.conn, S.PENDING_FILL):
            self.executor.cancel_order(row, reason="market_close")

    def _expire_limits(self, now: datetime) -> None:
        for row in orders.list_by_status(self.conn, S.PENDING_FILL):
            if row["expires_at"] and datetime.fromisoformat(
                    row["expires_at"]) < now:
                transitions.transition(self.conn, row["id"], S.CANCELLING, now)
                br = self.executor.broker.cancel(row)
                if br.status == "ok":
                    transitions.transition(self.conn, row["id"], S.EXPIRED, now)
                    self.activity.write(Category.TRADE, "limit_expired",
                                        f"{row['pair']}", ref_id=str(row["id"]))
                elif br.status == "rejected":
                    transitions.transition(self.conn, row["id"],
                                           S.PROTECTION_PENDING, now)
                else:
                    transitions.transition(self.conn, row["id"],
                                           S.CANCEL_UNKNOWN, now)

    def _maintain_reservations(self, now: datetime) -> None:
        """口座変動で維持できなくなった指値予約を約定前に取消す (設計書 §5)。"""
        account = accounting.current_account(self.conn, now)
        if account is None:
            return  # 発注側の fail closed に委ねる
        equity, _ = account
        if equity <= 0:
            # レビュー修正 6: equity<=0 だと notional/equity がゼロ除算になる。
            # ここでは何もせず、新規発注側の gate (fail closed) に委ねる。
            return
        risk = self.settings.risk
        while True:
            total_risk, notional, _ = open_risk_and_notional(
                self.conn, self.executor.spec_fn, risk)
            within = (total_risk <= equity * risk.max_total_risk_pct / 100
                      and notional / equity <= risk.max_leverage)
            if within:
                return
            pending = orders.list_by_status(self.conn, S.PENDING_FILL)
            if not pending:
                return  # 指値以外の超過は取消では解消できない
            newest = pending[-1]
            self.executor.cancel_order(newest, reason="reservation")

    def _force_close_day(self, now: datetime) -> None:
        if market_hours.next_rollover(now) - now > _DAY_CLOSE_BUFFER:
            return
        for row in orders.list_by_status(self.conn, S.OPEN):
            if row["horizon"] != "day":
                continue
            try:
                q = self.executor.quote_fn(row["pair"])
            except Exception as e:  # noqa: BLE001 — 架空価格で閉じない
                self.activity.write(Category.TRADE, "day_close_deferred",
                                    f"{row['pair']}: {e}", ref_id=str(row["id"]))
                continue
            price = q.bid if row["direction"] == "long" else q.ask
            self.executor.close_order(row, price, reason="day_rollover")

    def _process_fills(self, now: datetime) -> None:
        # レビュー修正 1: 同一 tick で新規に約定させた注文 (OPEN に遷移させた
        # もの) は第 2 ループで再取得しない。check_exit の
        # entry_same_bar=True 抑制 (同一バー内でのエントリー成立と TP 到達は
        # 順序判定不能) が、直後の第 2 ループで entry_same_bar=False として
        # 再評価されると無効化され、本来確定できないはずの TP が確定して
        # しまう欠陥があった。
        filled_ids: set[int] = set()
        for row in orders.list_by_status(self.conn, S.PENDING_FILL):
            bar = self._fresh_bar(row["pair"], now)
            if bar is None:
                continue
            price = check_limit_fill(row, bar, self._spread_for(row["pair"]))
            if price is None:
                continue
            transitions.transition(self.conn, row["id"], S.PROTECTION_PENDING,
                                   now, avg_fill_price=price,
                                   filled_quantity=row["quantity"],
                                   remaining_quantity=0.0,
                                   filled_at=now.isoformat())
            transitions.transition(self.conn, row["id"], S.OPEN, now)
            self.activity.write(Category.TRADE, "limit_filled",
                                f"{row['pair']} @{price}", ref_id=str(row["id"]))
            filled_ids.add(row["id"])
            # 同一バーで SL/TP に到達し得る → 保守則で即時判定
            filled = orders.get(self.conn, row["id"])
            self._check_one_exit(filled, bar, entry_same_bar=True)
        for row in orders.list_by_status(self.conn, S.OPEN):
            if row["id"] in filled_ids:
                continue  # 上のループで entry_same_bar=True 判定済み
            bar = self._fresh_bar(row["pair"], now)
            if bar is not None:
                self._check_one_exit(row, bar, entry_same_bar=False)
        # レビュー修正 5: 今 tick で見たバーを処理済みとして記録する走査対象
        # は、その時点の orders テーブルの状態 (PENDING_FILL/OPEN/CLOSED/
        # CANCELLED が存在する pair) ではなく、config で宣言されている全
        # ペアにする。元の実装は「この tick でこれら 4 状態のいずれの注文も
        # 無い pair」のバーを一切マーキングせず、状態集合のドリフト (期限
        # 切れのみ・unknown のみ等) に依存してしまっていた。
        for pair in self.settings.pairs:
            bar = self.bars_fn(pair)
            if bar is not None:
                self._processed_bar_ts[pair] = bar.ts

    def _spread_for(self, pair: str) -> float:
        """想定 spread (価格単位)。バーは方向非依存の系列なので、約定判定は
        long は ask 側 / short は bid 側へ half spread ずらして保守的に扱う。"""
        rule = self.settings.risk.pair_rules.get(pair)
        spec = self.executor.spec_fn(pair)
        return rule.assumed_spread_pips * spec.pip_size if rule else 0.0

    def _check_one_exit(self, row: dict, bar: Bar, *,
                        entry_same_bar: bool) -> None:
        if row["status"] != S.OPEN.value:
            return
        spread = self._spread_for(row["pair"])
        hit = check_exit(row, bar, spread, entry_same_bar=entry_same_bar)
        if hit is not None:
            kind, price = hit
            self.executor.close_order(row, price, reason=kind)

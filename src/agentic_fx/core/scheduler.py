"""Scheduler — クロック駆動 tick。mark-to-market・reconcile・予約維持・約定・毎時 Mission。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from agentic_fx.activity import ActivityLog, Category
from agentic_fx._safe_error import safe_error_text
from agentic_fx.config import Settings
from agentic_fx.core import accounting, market_hours, transitions
from agentic_fx.core.contracts import Bar, Mode, OrderStatus as S
from agentic_fx.core.executor import Executor, open_risk_and_notional
from agentic_fx.core.paper_fills import check_exit, check_limit_fill
from agentic_fx.store import orders
from agentic_fx.store.state import StateStore

_log = logging.getLogger("agentic_fx.scheduler")
_NEWS_INTERVAL = timedelta(minutes=30)
# 経済指標カレンダーは **週次** の JSON なので 30 分ごとに取り直しても無駄。
# ただし当日の forecast/previous は更新されうるので 1 日 1 回よりは細かく。
_ECON_INTERVAL = timedelta(hours=6)
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
                 on_news_cycle: Callable[[], None],
                 on_econ_cycle: Callable[[], None]) -> None:
        self.conn = conn
        self.executor = executor
        self.settings = settings
        self.state = state_store
        self.activity = activity
        self.bars_fn = bars_fn
        self.on_trade_mission = on_trade_mission
        self.on_news_cycle = on_news_cycle
        # on_econ_cycle は **デフォルト値を与えない** (キーワード必須)。
        # no-op のデフォルトを置くと「配線し忘れ」が無音で成立してしまい、
        # まさに cross-plan 欠陥② (EconCalendar.refresh の呼び出し元がどこ
        # にも無く econ_events が永久に空、例外も出ないのでエージェントが
        # 「重要指標の前ではない」と誤認する) が再発する。必須にしておけば
        # 未配線の呼び出し元は TypeError で列挙される。
        self.on_econ_cycle = on_econ_cycle
        self._last_trade: datetime | None = None
        self._last_news: datetime | None = None
        self._last_econ: datetime | None = None
        self._was_open: bool | None = None
        self._processed_bar_ts: dict[str, datetime] = {}

    def tick(self, now: datetime) -> None:
        # cross-plan 修正①: データ収集サイクルは tick の **冒頭** (開場判定
        # より前) で回す。
        # - 開場・閉場を 1 箇所でカバーでき、同じロジックの重複を作らない。
        #   以前はクローズ側ブロック (直後に return) の中にしか呼び出しが
        #   無く、市場は金 17:00 NY〜日 17:00 NY しか閉じないため、ニュース
        #   収集も RAG の 48h 掃除も **週末しか走らなかった** (実測: 14 日で
        #   news cycle は全部週末、最大間隔 5 日)。
        # - `_mark_to_market` の早期 return より前 = mark-to-market が失敗
        #   する tick でもニュースは取れる (ニュースは価格と独立)。
        # - `on_trade_mission` より前 = Mission が最新のニュースを読む。
        # 冒頭に置いた代償として、これらの経路の障害が _process_exits
        # (SL/TP 監視 = 資金保護) より前に立つ。だから呼び出しは必ず
        # _run_data_hook 経由 (fail-open) にすること。
        if self._last_news is None or now - self._last_news >= _NEWS_INTERVAL:
            self._last_news = now
            self._run_data_hook("news", self.on_news_cycle)
        if self._last_econ is None or now - self._last_econ >= _ECON_INTERVAL:
            self._last_econ = now
            self._run_data_hook("econ", self.on_econ_cycle)

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
            return
        self._was_open = True

        # レビュー修正 3: record_snapshot が時系列逆行 (NTP 補正等) で
        # ValueError を送出した場合、mark-to-market 自体が信頼できないため、
        # この tick は安全側に全体スキップする。
        if not self._mark_to_market(now):
            return
        self._resolve_unknowns(now)
        self._expire_limits(now)
        # codex 1: account snapshot が陳腐化/欠損 (current_account が None) の
        # 場合、総リスク・レバレッジを再検証できない。_maintain_reservations
        # が何もせず戻るだけでは、同じ tick で _process_fills が新たな建玉を
        # 生んでしまう (fail-open)。account が不明なら全 pending_fill を
        # 取消し、この tick の fills 処理はスキップする。
        account = accounting.current_account(self.conn, now)
        if account is None:
            self._cancel_pending_for_unknown_account(now)
        else:
            self._maintain_reservations(now, account)
        self._force_close_day(now)
        # 修正ラウンド 2: account が不明な tick は「新規約定」だけをスキップ
        # する (codex 1 の意図)。OPEN ポジションの SL/TP 監視
        # (_process_exits) は既存建玉の資金保護であり、口座情報の有無に
        # 関わらず必ず実行しなければならない — 以前は _process_fills 全体
        # (約定処理と SL/TP 監視の両方) を丸ごとスキップしており、口座陳腐化
        # 中は資金保護まで止まる回帰を生んでいた。
        filled_ids = self._process_limit_fills(now) if account is not None \
            else set()
        self._process_exits(now, filled_ids)
        if self._last_trade is None or now - self._last_trade >= timedelta(hours=1):
            self._last_trade = now
            self.on_trade_mission()

    # ---- internal -------------------------------------------------------

    def _run_data_hook(self, kind: str, fn: Callable[[], None]) -> None:
        """ニュース / econ の収集フックを fail-open で呼ぶ。

        cross-plan 修正②: これらは tick の冒頭 = `_process_exits`
        (SL/TP 監視 = 資金保護) より前に立つ。ニュース・経済指標は
        「欠損で取引を止めるデータではない」(fail-open) 一方、資金保護は
        絶対なので、収集経路の例外を tick に貫通させてはならない。

        ただし **無音では握り潰さない** — 技術ログ (warning) と activity の
        両方に残す。activity に出ないと恒常的な失敗が人の目に触れる経路が
        無くなる。例外テキストは `safe_error_text` を通す (フックの実体は
        外部 HTTP を叩くので、例外文字列に URL や API キーが載りうる)。

        SYSTEM カテゴリなのは、フックが例外を漏らした時点で **フック自身の
        fail-soft 契約が破れている** (= システム側の異常) から。ソース単位の
        通常の取得失敗は NewsCollector / EconCalendar が NEWS カテゴリで
        既に記録している。
        """
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — 資金保護を止めない (fail-open)
            text = safe_error_text(e)
            _log.warning("%s cycle failed: %s", kind, text)
            self.activity.write(Category.SYSTEM, f"{kind}_cycle_error", text)

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
            # 修正ラウンド 2: scheduler が直接呼ぶ broker.reconcile が例外を
            # 投げても、その注文だけスキップして次の注文の処理・tick 全体は
            # 継続する (executor 側は codex 2 で保護済みだが、scheduler が
            # 直接呼ぶ箇所は未保護だった — ここで例外が伝播すると tick の
            # 残り (SL/TP 監視含む) が全部飛ぶ)。
            try:
                br = self.executor.broker.reconcile(row)
            except Exception as e:  # noqa: BLE001
                self.activity.write(
                    Category.TRADE, "reconcile_error",
                    f"#{row['id']}: {e} — 次 tick 再試行",
                    ref_id=str(row["id"]))
                _log.warning("reconcile failed #%s: %s", row["id"], e)
                continue
            # codex 4: 終端 (rejected/cancelled) への変換は、broker が
            # 「注文なし」と明言した (status=="ok" かつ message=="not_found")
            # ときだけ行う。status=="ok" だけで終端化すると、将来の broker が
            # 「照会成功・注文はまだ存在する」を ok で返した場合に、実注文が
            # 残っているのに DB を終端にしてしまう。
            if br.status == "ok" and br.message == "not_found":
                to = resolution[row["status"]]
                transitions.transition(self.conn, row["id"], to, now)
                self.activity.write(Category.TRADE, "unknown_resolved",
                                    f"#{row['id']} -> {to.value}",
                                    ref_id=str(row["id"]))
            else:
                self.activity.write(
                    Category.TRADE, "reconcile_pending",
                    f"#{row['id']}: status={br.status} "
                    f"message={br.message!r} — 次 tick 再試行",
                    ref_id=str(row["id"]))
        # レビュー修正 2: CLOSING (今 tick 新規に遷移したものも、過去 tick から
        # quote 障害等で滞留しているものも含む) を毎 tick 再走査し、quote が
        # 復旧次第クローズを完結させる。executor._UNKNOWN 側にも CLOSING を
        # 含めているため、解決するまでは gate が新規発注を止め続ける。
        for row in orders.list_by_status(self.conn, S.CLOSING):
            self._retry_close(row, now)

    def _retry_close(self, row: dict, now: datetime) -> None:
        """CLOSING の再試行。

        修正ラウンド 3 (Important 相当の設計不整合の修正): 例外の発生源で
        扱いを変える必要がある。
        - ``quote_fn`` の例外は **broker に触れる前** に起きる → 未実行が
          確定しているので、状態を変えず次 tick 再試行してよい
          (quote 障害と同じ扱い)。
        - ``broker.close()`` の例外 (タイムアウト・接続断など) は
          **broker 側では成功しているかもしれない** → 「まだ close
          していない」とみなして黙って再試行するのは、Phase 3 の MT5 で
          reconcile せず盲目的に再 close する二重クローズのリスクになる。
          既存の `br2.status != "ok"` 分岐と同じ `S.CLOSE_UNKNOWN` に
          遷移させ、次 tick の reconcile 経路 (`_resolve_unknowns`) に
          乗せなければならない。
        """
        try:
            q = self.executor.quote_fn(row["pair"])
        except Exception as e:  # noqa: BLE001 — broker 未接触。次 tick 再試行
            self.activity.write(Category.TRADE, "close_retry_deferred",
                                f"{row['pair']}: {e}", ref_id=str(row["id"]))
            _log.warning("close retry deferred (quote) #%s: %s", row["id"], e)
            return
        price = q.bid if row["direction"] == "long" else q.ask
        fresh = orders.get(self.conn, row["id"])
        try:
            br2 = self.executor.broker.close(fresh, price, "retry")
        except Exception as e:  # noqa: BLE001 — broker 側は成功済みかもしれない
            transitions.transition(self.conn, row["id"], S.CLOSE_UNKNOWN, now)
            self.activity.write(Category.TRADE, "close_unknown",
                                f"{row['pair']} — reconcile 待ち "
                                f"(broker error: {e})", ref_id=str(row["id"]))
            self.executor.notifier.send(
                f"[agentic-fx] クローズ結果不明 #{row['id']}")
            _log.warning("close retry -> close_unknown (broker error) "
                        "#%s: %s", row["id"], e)
            return
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

    def _cancel_pending_for_unknown_account(self, now: datetime) -> None:
        """codex 1: current_account が陳腐化/欠損している場合、総リスク・
        レバレッジを再検証できないまま指値を約定させるのは fail-open。
        全 pending_fill を取消し (取消結果が cancel_unknown になったものは
        そのまま新規発注停止に接続される)、この tick は約定処理をしない。"""
        pending = orders.list_by_status(self.conn, S.PENDING_FILL)
        if pending:
            self.activity.write(
                Category.SYSTEM, "account_unknown_cancel_pending",
                f"{len(pending)} 件の pending_fill を取消 "
                "(口座 snapshot が陳腐化/欠損 — 総リスク再検証不能)")
        for row in pending:
            self.executor.cancel_order(row, reason="account_unknown")

    def _maintain_reservations(self, now: datetime,
                               account: tuple[float, float]) -> None:
        """口座変動で維持できなくなった指値予約を約定前に取消す (設計書 §5)。"""
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
        """codex 3: 期限は注文毎に (filled_at 起点の next_rollover で) 導出
        する。tick 全体の「次の rollover まで 5 分以内」だけを条件にすると、
        20:55-20:59 に quote 障害が続き 21:00 を過ぎた途端に
        next_rollover(now) が翌日を指してしまい、day ポジションが対象から
        外れて約 24 時間 (金曜なら週末) 残ってしまう。注文毎の期限を過ぎた
        後もこの条件は真であり続けるため、quote が復旧し次第、毎 tick
        再試行される。quote 取得失敗時に架空価格で閉じない挙動は維持する。
        """
        for row in orders.list_by_status(self.conn, S.OPEN):
            if row["horizon"] != "day":
                continue
            anchor = row["filled_at"] or row["created_at"]
            deadline = market_hours.next_rollover(datetime.fromisoformat(anchor))
            if now < deadline - _DAY_CLOSE_BUFFER:
                continue
            try:
                q = self.executor.quote_fn(row["pair"])
            except Exception as e:  # noqa: BLE001 — 架空価格で閉じない
                self.activity.write(Category.TRADE, "day_close_deferred",
                                    f"{row['pair']}: {e}", ref_id=str(row["id"]))
                continue
            price = q.bid if row["direction"] == "long" else q.ask
            self.executor.close_order(row, price, reason="day_rollover")

    def _process_limit_fills(self, now: datetime) -> set[int]:
        """PENDING_FILL の約定処理。約定させた注文 ID の集合を返す。

        修正ラウンド 2: account が不明な tick では呼ばない (tick() 側で
        判定する) — 総リスク・レバレッジを再検証できない状態で新規建玉を
        作らないため (codex 1)。SL/TP 監視 (`_process_exits`) とは別関数に
        分離した (口座不明時に約定処理だけ止め、既存ポジションの保護は
        止めないため)。
        """
        # レビュー修正 1: 同一 tick で新規に約定させた注文 (OPEN に遷移させた
        # もの) は _process_exits 側で再評価しない。check_exit の
        # entry_same_bar=True 抑制 (同一バー内でのエントリー成立と TP 到達は
        # 順序判定不能) が、直後に entry_same_bar=False として再評価される
        # と無効化され、本来確定できないはずの TP が確定してしまう欠陥が
        # あった。
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
        return filled_ids

    def _process_exits(self, now: datetime, filled_ids: set[int]) -> None:
        """OPEN ポジションの SL/TP 到達判定。

        修正ラウンド 2 (Major 回帰の修正): 既存ポジションの資金保護は
        口座 snapshot の有無に関わらず**必ず**実行する。以前は
        `_process_fills` 一つの関数の中で約定処理と SL/TP 監視を両方行って
        おり、account 不明時に呼び出し自体をスキップしていたため、口座
        陳腐化中は建玉の SL/TP 監視まで丸ごと止まってしまっていた
        (絶対制約「資金保護は決定論的コードで強制する」に抵触)。
        """
        for row in orders.list_by_status(self.conn, S.OPEN):
            if row["id"] in filled_ids:
                continue  # 同一 tick で約定済み — entry_same_bar=True 判定済み
            bar = self._fresh_bar(row["pair"], now)
            if bar is not None:
                self._check_one_exit(row, bar, entry_same_bar=False)
        # レビュー修正 5: 今 tick で見たバーを処理済みとして記録する走査対象
        # は、その時点の orders テーブルの状態 (PENDING_FILL/OPEN/CLOSED/
        # CANCELLED が存在する pair) ではなく、config で宣言されている全
        # ペアにする。元の実装は「この tick でこれら 4 状態のいずれの注文も
        # 無い pair」のバーを一切マーキングせず、状態集合のドリフト (期限
        # 切れのみ・unknown のみ等) に依存してしまっていた。マーキングは
        # account の有無に関わらず (_process_limit_fills が呼ばれなかった
        # tick でも) 必ず行う。
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

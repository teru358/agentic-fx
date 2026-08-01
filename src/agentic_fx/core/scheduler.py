"""Scheduler — クロック駆動 tick。mark-to-market・reconcile・予約維持・約定・毎時 Mission。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from agentic_fx.activity import ActivityLog, Category
from agentic_fx._safe_error import safe_error_text, safe_text
from agentic_fx.config import Settings
from agentic_fx.core import accounting, market_hours, transitions
from agentic_fx.core.contracts import Bar, ConversionRate, Mode, OrderStatus as S
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
                 on_trade_mission: Callable[[str], None],
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
        """1 tick 分の決定論的処理。

        **呼び出し契約 (codex C-M5)**: `tick()` は **単一スレッド・単一
        タイマーからの逐次呼び出し**を前提とする。再入ガード (ロック) は
        **持たない** — 同時に 2 本走らせると、`_processed_bar_ts` の
        read-modify-write や「取消 → 約定スキップ」の 2 段構えが
        インターリーブし、同じバーの二重約定や取消と約定の競合を生む。
        直列化はプロセス側の責務で、service 配線が `core_lock` の下に
        置く (プラン 5)。ここでロックを持たないのは、tick と裁量操作
        (Mission 由来の発注/クローズ) を **同じ**ロックで直列化する必要が
        あり、scheduler 内部の自前ロックではその範囲を張れないため。
        """
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
        # codex C-I1: equity<=0 (債務超過) は account 欠損と **同等以上** に
        # 扱う。以前は _maintain_reservations が equity<=0 で早期 return し
        # 「新規発注側の gate (fail closed) に委ねる」としていたが、gate は
        # **新規発注時にしか走らない** — 予約済み指値が約定する経路
        # (_process_limit_fills) では再実行されないので、到達バーが来れば
        # 債務超過のまま OPEN が生まれてしまう (fail-open)。account 欠損と
        # 同じ経路で全 pending_fill を取消し、この tick の約定処理も飛ばす。
        account = accounting.current_account(self.conn, now)
        fills_allowed = account is not None and account[0] > 0
        if account is None:
            self._cancel_all_pending(
                now, reason="account_unknown",
                event="account_unknown_cancel_pending",
                why="口座 snapshot が陳腐化/欠損 — 総リスク再検証不能")
        elif account[0] <= 0:
            self._cancel_all_pending(
                now, reason="equity_nonpositive",
                event="equity_nonpositive_cancel_pending",
                why="equity<=0 (債務超過) — 予約を維持できない")
        else:
            # 新規 Important (fix round 2): _maintain_reservations が
            # リスク超過の予約取消に失敗して中断 (C1 の except -> return)
            # した場合でも、ここで fills_allowed を False に落とさないと、
            # その予約が同一 tick の _process_limit_fills で (取消対象と
            # 判定されたにも関わらず) 到達バーで約定してしまう fail-open
            # 窓が開く (gate は予約約定経路では再実行されないため —
            # レビュアー実測)。account 不明 / equity<=0 と同じ 2 層構えに
            # 揃え、_maintain_reservations の戻り値 (bool) で判定する。
            #
            # 新規 Critical site 1 (fix round 3): _maintain_reservations
            # 呼び出し自体も無保護だった。内部の open_risk_and_notional は
            # _EXPOSURE 全行に spec_fn(pair) を回すため、1 件の予約/建玉が
            # spec を引けないペア (DataUnhealthy 等) だと、呼び出しが
            # 無保護なら tick 全体が死ぬ (レビュアー実測: GBPJPY の
            # PENDING_FILL 1 件 + spec_fn が GBPJPY のみ raise + USDJPY
            # OPEN の SL 未執行)。取消失敗時と同じ 2 層構えに揃え、例外時は
            # fills_allowed=False にして後続の _process_exits (資金保護)
            # を必ず生かす。
            try:
                if not self._maintain_reservations(now, account):
                    fills_allowed = False
            except Exception as e:  # noqa: BLE001 — 資金保護を止めない
                text = safe_error_text(e)
                self.activity.write(
                    Category.TRADE, "maintain_reservations_error",
                    f"{text} — この tick の予約維持処理を中断")
                _log.warning("maintain_reservations failed: %s", text)
                fills_allowed = False
        self._force_close_day(now)
        # 修正ラウンド 2: account が不明な tick は「新規約定」だけをスキップ
        # する (codex 1 の意図)。OPEN ポジションの SL/TP 監視
        # (_process_exits) は既存建玉の資金保護であり、口座情報の有無に
        # 関わらず必ず実行しなければならない — 以前は _process_fills 全体
        # (約定処理と SL/TP 監視の両方) を丸ごとスキップしており、口座陳腐化
        # 中は資金保護まで止まる回帰を生んでいた。
        #
        # 2 層構え (codex C-I1): 1 層目 = 上の全 pending 取消、2 層目 = ここの
        # スキップ。1 層目の取消が broker 側で rejected/unknown になると行は
        # PENDING_FILL に残らないが、取消経路そのものが例外や将来の変更で
        # 効かなくなった場合に備えて、約定側にも独立した条件を置く。
        filled_ids = self._process_limit_fills(now) if fills_allowed else set()
        self._process_exits(now, filled_ids)
        reason = self._trade_mission_due(now)
        if reason is not None:
            self._last_trade = now
            self.on_trade_mission(reason)

    # ---- internal -------------------------------------------------------

    def _trade_mission_due(self, now: datetime) -> str | None:
        """毎時 Mission の起動要否と起動理由を返す (上書き 1 — 設計書改訂 5)。

        現状の起動条件は cron (1 時間毎) のみなので理由は常に ``"cron"``。
        戻り値を `str | None` にしておくことで、将来トリガー種別が増えても
        `tick()` 側を変更せずに済む (missions.trigger の監査列を殺さないため
        `on_trade_mission` には必ず理由を渡す)。
        """
        if self._last_trade is None or now - self._last_trade >= timedelta(hours=1):
            return "cron"
        return None

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

        codex C-I2: 評価の本体は広い try で包む。`bars_fn` (= latest_1m_bar)
        は `DataUnhealthy` を握って None を返すので典型的なデータ不健全は
        既に安全だが、`broker.equity()` / `spec_fn` (未知ペアの
        DataUnhealthy) / sqlite3.Error 等の**想定外例外**は貫通し、
        tick 全体 = `_process_exits` (SL/TP 監視 = 資金保護) を殺していた。
        想定外例外では snapshot を記録せず **tick は継続** する
        (snapshot が止まれば current_account がやがて陳腐化し、新規約定側は
        自動的に fail closed になる — 止まってはいけないのは保護の側だけ)。
        """
        try:
            balance, unrealized, stale = self._evaluate_positions(now)
        except Exception as e:  # noqa: BLE001 — 資金保護を止めない
            text = safe_error_text(e)
            self.activity.write(Category.SYSTEM, "mark_to_market_error",
                                f"{text} — snapshot 見送り (tick は継続)")
            _log.warning("mark-to-market failed: %s", text)
            return True
        if stale:
            return True  # snapshot は記録しないが tick は継続する
        # レビュー修正 3 の裁定 (時系列逆行 = 時計異常 → tick 全体スキップ)
        # は **上の広い except の外** に置く。中に入れると、将来 except の
        # 側を触ったときに「時計異常でも tick 継続」へ無音で変質しうる。
        try:
            accounting.record_snapshot(self.conn, now=now, balance=balance,
                                       equity=balance + unrealized)
        except ValueError as e:
            text = safe_error_text(e)
            self.activity.write(Category.SYSTEM, "snapshot_out_of_order", text)
            _log.warning("mark-to-market skipped (clock regression?): %s", text)
            return False
        except Exception as e:  # noqa: BLE001 — DB 障害でも資金保護は止めない
            text = safe_error_text(e)
            self.activity.write(Category.SYSTEM, "mark_to_market_error",
                                f"{text} — snapshot 見送り (tick は継続)")
            _log.warning("record_snapshot failed: %s", text)
            return True
        return True

    def _evaluate_positions(self, now: datetime) -> tuple[float, float, bool]:
        """(balance, unrealized, stale) を返す。例外は呼び出し側が握る。

        設計書 §5: 時価評価はポジション毎に quote→account 換算してから
        合算する。**いずれかのポジションのレートが解決できない tick は
        snapshot を記録しない** — バー陳腐化と同じ扱い (stale=True)。
        1 回の判断 (このサイクル) 内でレートを固定する (`cycle_rate_fn`) —
        このメソッドの呼び出しをまたいで使い回さない (毎 tick 新規に作る)。
        """
        balance, _ = self.executor.broker.equity()
        unrealized = 0.0
        stale = False
        cycle_rate = self.executor.cycle_rate_fn(now)
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
            try:
                rate = cycle_rate(spec.quote_currency)
            except Exception as e:  # noqa: BLE001 — バー陳腐化と同じ扱い
                stale = True
                text = safe_error_text(e)
                self.activity.write(
                    Category.SYSTEM, "snapshot_stale_rate_skip",
                    f"{row['pair']}: {text} — 時価評価を見送り",
                    ref_id=str(row["id"]))
                _log.warning("mark-to-market rate unavailable for %s: %s",
                            row["pair"], text)
                continue
            sign = 1.0 if row["direction"] == "long" else -1.0
            unrealized += (bar.close - row["avg_fill_price"]) \
                * spec.contract_size * (row["quantity"] or 0.0) * sign \
                * rate.value
        return balance, unrealized, stale

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
                # codex C-I3: 例外テキストは必ず safe_error_text を通す。
                # 現状の broker は paper なので実害は薄いが、Phase 3 で
                # broker が MT5 bridge (httpx) になると、この文字列に
                # bridge の URL がそのまま載って activity / 通知に流れる。
                text = safe_error_text(e)
                self.activity.write(
                    Category.TRADE, "reconcile_error",
                    f"#{row['id']}: {text} — 次 tick 再試行",
                    ref_id=str(row["id"]))
                _log.warning("reconcile failed #%s: %s", row["id"], text)
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
                # codex C-I3 (5 箇所の列挙に無かった同型の経路): br.message は
                # **broker が返す外部由来のテキスト**。Phase 3 の MT5 bridge
                # がエラー本文に自分のエンドポイントを載せれば、ここから
                # activity に URL が出る。safe_text を通す。
                self.activity.write(
                    Category.TRADE, "reconcile_pending",
                    f"#{row['id']}: status={br.status} "
                    f"message={safe_text(str(br.message))!r} — 次 tick 再試行",
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
            text = safe_error_text(e)   # codex C-I3
            self.activity.write(Category.TRADE, "close_retry_deferred",
                                f"{row['pair']}: {text}", ref_id=str(row["id"]))
            _log.warning("close retry deferred (quote) #%s: %s", row["id"],
                         text)
            return
        price = q.bid if row["direction"] == "long" else q.ask
        fresh = orders.get(self.conn, row["id"])
        try:
            br2 = self.executor.broker.close(fresh, price, "retry")
        except Exception as e:  # noqa: BLE001 — broker 側は成功済みかもしれない
            text = safe_error_text(e)   # codex C-I3
            transitions.transition(self.conn, row["id"], S.CLOSE_UNKNOWN, now)
            self.activity.write(Category.TRADE, "close_unknown",
                                f"{row['pair']} — reconcile 待ち "
                                f"(broker error: {text})", ref_id=str(row["id"]))
            self.executor.notifier.send(
                f"[agentic-fx] クローズ結果不明 #{row['id']}")
            _log.warning("close retry -> close_unknown (broker error) "
                        "#%s: %s", row["id"], text)
            return
        if br2.status == "ok":
            # broker 側は成功済み — 以降の DB 確定処理の失敗は握りつぶさない
            from agentic_fx.core.paper_broker import compute_pnl
            spec = self.executor.spec_fn(row["pair"])
            # 設計書 §5: クローズはレート欠損でも妨げない (close_order と
            # 同じ degraded フォールバック規律)。
            rate, degraded = self.executor.resolve_close_rate(
                spec.quote_currency, now)
            pnl = compute_pnl(
                fresh, price, contract_size=spec.contract_size,
                commission_per_lot=self.settings.risk.commission_per_lot,
                quote_to_account_rate=rate.value) if rate is not None else None
            transitions.transition(
                self.conn, row["id"], S.CLOSED, now,
                close_price=price, realized_pnl=pnl, closed_at=now.isoformat())
            if degraded:
                self.activity.write(
                    Category.TRADE, "close_pnl_rate_degraded",
                    f"{row['pair']}: 換算レート取得不能 — " + (
                        "最後の健全レートで計算 (次回同期で吸収)"
                        if pnl is not None
                        else "realized_pnl 未確定 (次回同期で解消)"),
                    ref_id=str(row["id"]))
                self.executor.notifier.send(
                    f"[agentic-fx] クローズ換算レート degraded #{row['id']}")
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
            # I2 (fix round 1, advisor 指摘で追加): broker が結論
            # (br.status) を返した**後**の失敗と、broker に結論を出させる
            # 前の失敗を区別するフラグ。前者で無条件に CANCEL_UNKNOWN へ
            # 落とすと、broker が "rejected" (= 既に約定済み/保護済み) と
            # 教えてくれた注文が、こちら側の DB 書き込み失敗のせいで
            # reconcile 経由 (not_found) で黙って cancelled になり得る
            # (資金保護のハザード — Phase 1 の PaperBroker は "ok" しか
            # 返さないため未到達だが、Phase 3 の MT5 で "rejected"/
            # "unknown" が現実になる)。_retry_close が CLOSE_UNKNOWN を
            # 「broker 未接触/結果不明」専用にしているのと同じ規律に揃える。
            broker_answered = False
            # N3/I1 (fix round 1): 期限判定 (datetime.fromisoformat) 自体も
            # 隔離の内側に置く。以前は try の外にあり、不正な expires_at
            # (ISO でない文字列等) の行が 1 件あるだけで毎 tick 同じ行で
            # 例外を吐き、資金保護が恒久停止していた (レビュアー実測)。
            # 注文単位で隔離する (_cancel_all_pending / _process_limit_fills
            # / _process_exits と同型パターン)。
            try:
                if not (row["expires_at"] and datetime.fromisoformat(
                        row["expires_at"]) < now):
                    continue
                transitions.transition(self.conn, row["id"], S.CANCELLING, now)
                br = self.executor.broker.cancel(row)
                broker_answered = True
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
            except Exception as e:  # noqa: BLE001 — 他の期限切れ注文を止めない
                text = safe_error_text(e)
                # I2 (fix round 1): 以前の文言「— 次 tick 再試行」は事実に
                # 反していた。broker.cancel 例外後の行は CANCELLING に
                # 留まり、_expire_limits の PENDING_FILL 走査にも
                # _resolve_unknowns の走査集合 (_UNKNOWN) にも入らず、
                # 誰も再試行しない (gate も止まらない一方、_EXPOSURE には
                # 算入されリスク枠を恒久占有する — 実測)。CANCEL_UNKNOWN へ
                # 遷移させ _resolve_unknowns の走査に乗せる。
                #
                # ただし broker_answered が True (= broker が結論を返した
                # 後の失敗) の場合はこの遷移を試みない — 上のコメント参照。
                # broker が返した結論を無視して CANCELLING に留めておく方が、
                # 誤って cancelled にするより安全側。
                #
                # 遷移の罠: 例外源が CANCELLING 遷移そのものだった場合、
                # 行はまだ PENDING_FILL のままで ALLOWED[PENDING_FILL] に
                # CANCEL_UNKNOWN は無く、無条件に遷移を試みると
                # IllegalTransition がこのハンドラから漏れる。現在状態を
                # 読み直し、CANCEL_UNKNOWN へ遷移可能な場合のみ試みる。
                # 遷移呼び出し自体も独自の try で包み、どんな失敗でも
                # この隔離を破らない。
                note = "reconcile 未実施 — 手動確認が必要な場合あり"
                if not broker_answered:
                    # 新規 Critical (fix round 2): 以前はこの復旧処理の
                    # うち transitions.transition だけを内側 try で
                    # 包んでおり、orders.get / S(current["status"]) は
                    # 無保護のままだった。外側例外 (broker.cancel の
                    # 例外) の最も自然な源は DB 障害であり、**同じ conn**
                    # を使うこの orders.get も同じ理由で落ちうる
                    # (レビュアー実測: sqlite3.OperationalError で SL
                    # 未執行)。ここで例外が漏れると _expire_limits に
                    # 外側 try が無いため tick を貫通し、後続の
                    # _process_exits (SL/TP 監視) が丸ごと死ぬ。
                    # orders.get / S(...) / 遷移をまとめて 1 つの try で
                    # 包み、どんな失敗でもこの隔離を破らないようにする。
                    try:
                        current = orders.get(self.conn, row["id"])
                        cur_status = (S(current["status"])
                                     if current else None)
                        if (cur_status is not None and S.CANCEL_UNKNOWN
                                in transitions.ALLOWED.get(cur_status,
                                                           frozenset())):
                            transitions.transition(self.conn, row["id"],
                                                   S.CANCEL_UNKNOWN, now)
                            note = ("cancel_unknown へ遷移 — 次 tick の "
                                   "reconcile で解決")
                    except Exception as trans_err:  # noqa: BLE001 —
                        # 隔離を破らない (復旧処理の失敗でもこの注文を
                        # スキップするのみ)
                        _log.warning(
                            "limit expire -> cancel_unknown failed "
                            "#%s: %s", row["id"],
                            safe_error_text(trans_err))
                self.activity.write(
                    Category.TRADE, "limit_expire_error",
                    f"#{row['id']} {row['pair']}: {text} — {note}",
                    ref_id=str(row["id"]))
                _log.warning("limit expire failed #%s: %s", row["id"], text)

    def _cancel_all_pending(self, now: datetime, *, reason: str, event: str,
                            why: str) -> None:
        """予約済み指値を「約定させてはいけない」状態で全取消しする共通経路。

        codex 1: current_account が陳腐化/欠損している場合、総リスク・
        レバレッジを再検証できないまま指値を約定させるのは fail-open。
        codex C-I1: equity<=0 (債務超過) も同じ — むしろこちらは口座値が
        分かっていて「維持できないと確定している」ぶん強い。どちらも
        全 pending_fill を取消し (取消結果が cancel_unknown になったものは
        そのまま新規発注停止に接続される)、この tick は約定処理をしない。

        `reason` は orders.close_reason に載る (取消理由が activity と DB の
        両方で区別できるよう、原因ごとに別の値を渡す)。
        """
        pending = orders.list_by_status(self.conn, S.PENDING_FILL)
        if pending:
            self.activity.write(
                Category.SYSTEM, event,
                f"{len(pending)} 件の pending_fill を取消 ({why})")
        for row in pending:
            # N3: この関数は equity<=0 (債務超過 = SL/TP が最も要る状態) で
            # 呼ばれ、_process_exits より **前** に立つ。1 件の cancel_order
            # 例外で以降の取消と後続の資金保護が全部止まってはならない —
            # 注文単位で隔離する (_process_limit_fills / _process_exits と
            # 同型パターン)。
            try:
                self.executor.cancel_order(row, reason=reason)
            except Exception as e:  # noqa: BLE001 — 他注文の取消を止めない
                text = safe_error_text(e)
                self.activity.write(
                    Category.TRADE, "cancel_all_pending_error",
                    f"#{row['id']} {row['pair']}: {text} — 次 tick 再試行",
                    ref_id=str(row["id"]))
                _log.warning("cancel_all_pending failed #%s: %s", row["id"],
                            text)

    def _maintain_reservations(self, now: datetime,
                               account: tuple[float, float]) -> bool:
        """口座変動で維持できなくなった指値予約を約定前に取消す (設計書 §5)。

        戻り値 (fix round 2): **True** = 通常終了 (超過なし、または
        解消できた/pending 無し)。**False** = cancel_order の例外で
        トリミングを中断した場合 — 呼び出し元 tick() は**この tick の
        fills_allowed を False にしなければならない**。

        以前は戻り値を持たず、except -> return (C1 の無限ループ回避) だけ
        していた。しかしそれだけだと、リスク超過と判定され取消対象に
        上がった予約が、cancel_order 失敗で PENDING_FILL に残ったまま
        fills_allowed=True の tick で _process_limit_fills に処理され、
        到達バーがあれば約定してしまう fail-open 窓が開く (gate は予約
        約定経路では再実行されないため — レビュアー実測)。account 不明 /
        equity<=0 のときの 2 層構え (全 pending 取消 + fills_allowed=False)
        に揃え、`_process_exits` (資金保護) は影響を受けず通常どおり走る。
        """
        equity, _ = account
        if equity <= 0:
            # レビュー修正 6: equity<=0 だと notional/equity がゼロ除算になる。
            # codex C-I1: 呼び出し側 (tick) が equity<=0 を先に捌く
            # (全 pending 取消 + 約定スキップ) ので、ここは到達しない想定の
            # 保険。**「gate の fail closed に委ねる」は誤り**だった —
            # gate は新規発注時にしか走らず、予約済み指値の約定では
            # 再実行されない。ゼロ除算だけは常に避ける。
            return True
        risk = self.settings.risk
        # 設計書 §5: 1 回の予約再検証サイクル (この呼び出し全体、while ループ
        # を含む) 内でレートを固定する。呼び出しをまたいで使い回さない
        # (毎 tick 新規に作る) — 「予約再検証はその時点の現在レートで行う」
        # という運用方針と一貫させるため。
        cycle_rate = self.executor.cycle_rate_fn(now)
        ok = self._cancel_pairs_with_unavailable_rate(cycle_rate)
        while True:
            total_risk, notional, _ = open_risk_and_notional(
                self.conn, self.executor.spec_fn, risk, cycle_rate)
            within = (total_risk <= equity * risk.max_total_risk_pct / 100
                      and notional / equity <= risk.max_leverage)
            if within:
                return ok
            pending = orders.list_by_status(self.conn, S.PENDING_FILL)
            if not pending:
                return ok  # 指値以外の超過は取消では解消できない
            newest = pending[-1]
            # C1 (fix round 1): この while True ループは「予約が 1 件
            # 減って条件を再評価する」進行を cancel_order の成功
            # (CANCELLING への遷移) に依存している。ここで try/except →
            # continue を当てると、cancel_order が同じ行で毎回 raise する
            # 限り同じ行を掴み続けて無限ループ (tick ハング) になる
            # (レビュアー実測)。他の隔離 site と異なり **return** して
            # この tick の予約トリミングを諦め、後続の _process_exits
            # (資金保護) に処理を渡す。
            try:
                self.executor.cancel_order(newest, reason="reservation")
            except Exception as e:  # noqa: BLE001 — 資金保護 (SL/TP 監視) は
                # この関数の外で続く。ここで諦めるのは「予約トリミング」
                # だけであり、無限ループより安全側。
                text = safe_error_text(e)
                # I2 と同根の注意: cancel_order 内部の遷移が途中まで進んで
                # いた場合、この行は PENDING_FILL から外れている可能性が
                # あり (例: CANCELLING で停止)、その場合「次 tick 再試行」
                # は事実に反する (次 tick の走査対象から漏れうる)。ここでは
                # 断定を避け、事実 (この tick では打ち切った) だけを書く。
                self.activity.write(
                    Category.TRADE, "reservation_trim_error",
                    f"#{newest['id']} {newest['pair']}: {text} "
                    "— この tick の予約トリミングを打ち切り",
                    ref_id=str(newest["id"]))
                _log.warning("reservation trim failed #%s: %s",
                            newest["id"], text)
                # 新規 Important (fix round 2): False を返し、呼び出し元
                # tick() にこの tick の fills_allowed=False を委ねる
                # (上の docstring 参照)。
                return False

    def _cancel_pairs_with_unavailable_rate(
            self, cycle_rate: Callable[[str], ConversionRate]) -> bool:
        """設計書 §5「レート取得不能時の層別動作」: 予約再検証で当該ペアの
        口座通貨換算ができない場合、そのペアの pending_fill を取消す
        (ペア単位。他ペアの予約は健全なレートで再検証を継続する)。

        **`spec_fn(pair)` の例外はここでは保護しない** (既存の site1 系
        テストが固定する挙動: spec 自体が引けない場合は「換算不能」とは
        別の障害クラスであり、呼び出し元 `_maintain_reservations` /
        `open_risk_and_notional` を通じて tick() の広い except に落ちる
        既存の全体フォールバックに委ねる — ここで拾って握ると、
        「取消は諦められた」という既存の観測可能な挙動が変わってしまう)。
        保護するのは `cycle_rate` (= レート取得) の失敗だけ。

        戻り値: すべてのペアの取消が (取消不能ペアが無ければ) 成功した場合
        True。取消を試みて失敗した (cancel_order が CANCELLED 以外に終わる、
        または例外を投げる) 場合は False — 呼び出し元はこの tick の
        fills_allowed を False にする (資金保護の 2 層構えと同じ規律)。

        fix round 2 (codex 節目レビュー): `cycle_rate` は判断内の全レート
        leg 時刻の running span も検証する (`Executor.cycle_rate_fn` 参照)。
        そのため、あるペアの `cycle_rate` 呼び出しが「このペア自身のレート
        個別には健全だが、既に取得済みの別ペアのレートと合わせた全体
        スパンを破る」ことで失敗しうる。**先に処理され既にキャッシュ済みの
        ペアは影響を受けず、後から処理されたペアの方が取消される** — この
        関数はペアを `sorted()` で決定的な順序で処理し、「どのペアが破った
        側として扱われるか」を再現可能にする。取消は破った側 1 ペアのみに
        限定し、既に確定した他ペアの予約は取消さない (「当該ペアのみ取消・
        他ペアは継続」という既存セマンティクスを維持)。
        """
        ok = True
        pairs = sorted({row["pair"]
                       for row in orders.list_by_status(self.conn,
                                                        S.PENDING_FILL)})
        for pair in pairs:
            spec = self.executor.spec_fn(pair)   # 保護しない (docstring 参照)
            try:
                cycle_rate(spec.quote_currency)
                cycle_rate(spec.base_currency)
            except Exception as e:  # noqa: BLE001 — 換算不能はペア単位取消
                text = safe_error_text(e)
                self.activity.write(
                    Category.TRADE, "reservation_rate_unavailable",
                    f"{pair}: {text} — 当該ペアの pending_fill を取消")
                _log.warning("reservation rate unavailable for %s: %s",
                            pair, text)
                for row in orders.list_by_status(self.conn, S.PENDING_FILL):
                    if row["pair"] != pair:
                        continue
                    try:
                        final = self.executor.cancel_order(
                            row, reason="rate_unavailable")
                        if final != S.CANCELLED:
                            ok = False
                    except Exception as cancel_err:  # noqa: BLE001
                        ok = False
                        ctext = safe_error_text(cancel_err)
                        self.activity.write(
                            Category.TRADE, "reservation_rate_cancel_error",
                            f"#{row['id']} {pair}: {ctext} — この tick の"
                            "取消を打ち切り", ref_id=str(row["id"]))
                        _log.warning(
                            "reservation rate cancel failed #%s: %s",
                            row["id"], ctext)
        return ok

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
            # site 2 (fix round 3): ポジション単位で隔離する。以前は
            # datetime.fromisoformat(anchor) (I1 と同型の欠陥) も
            # self.executor.close_order(...) も無保護で、1 件のポジション
            # の例外 (close_order 内の spec_fn が DataUnhealthy で raise
            # する等) が _force_close_day → tick を貫通し、後続の
            # _process_exits (SL/TP 監視 = 資金保護) を丸ごと止めていた
            # (レビュアー実測)。
            try:
                anchor = row["filled_at"] or row["created_at"]
                deadline = market_hours.next_rollover(
                    datetime.fromisoformat(anchor))
                if now < deadline - _DAY_CLOSE_BUFFER:
                    continue
                try:
                    q = self.executor.quote_fn(row["pair"])
                except Exception as e:  # noqa: BLE001 — 架空価格で閉じない
                    text = safe_error_text(e)   # codex C-I3
                    self.activity.write(Category.TRADE, "day_close_deferred",
                                        f"{row['pair']}: {text}",
                                        ref_id=str(row["id"]))
                    continue
                price = q.bid if row["direction"] == "long" else q.ask
                self.executor.close_order(row, price, reason="day_rollover")
            except Exception as e:  # noqa: BLE001 — 他ポジションの day 強制
                # 決済・後続の _process_exits (資金保護) を止めない。
                text = safe_error_text(e)
                # 注文状態がどちらに転んでも次 tick に必ず再走査される
                # ため「次 tick 再試行」は事実に即している (I2 の教訓):
                # ①close_order の例外源が spec_fn や CLOSING 遷移そのもの
                # なら行は OPEN のまま残り、この関数自身が次 tick も
                # day 判定を再実行する。②CLOSING まで進んだ後の失敗
                # (broker.close 後の DB 確定処理等) なら、CLOSING は
                # _resolve_unknowns の _retry_close の走査対象であり、
                # 次 tick に必ず拾われる (I2 の CANCELLING のような
                # 「誰の走査集合にも入らない」状態にはならない)。
                # このため except ハンドラ内で orders.get 等の復旧処理を
                # 追加していない — F1 の教訓 (復旧処理自体が外側例外と
                # 同じ故障源で落ちる) をそもそも踏まない設計にした。
                self.activity.write(
                    Category.TRADE, "day_close_error",
                    f"#{row['id']} {row['pair']}: {text} — 次 tick 再試行",
                    ref_id=str(row["id"]))
                _log.warning("day close failed #%s: %s", row["id"], text)

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
            # codex C-I2: この関数は _process_exits (資金保護) の **前** に
            # 走るので、1 注文分の例外 (bars_fn / spec_fn の想定外例外、
            # DB 障害等) がここを貫通すると、既存建玉の SL/TP 監視ごと
            # 飛んでしまう。注文単位で隔離する。
            #
            # filled_ids.add は例外を投げうる呼び出し (_check_one_exit) の
            # **前** に置く: 約定が成立した後で例外を握った場合、その注文が
            # filled_ids に入っていないと _process_exits が
            # entry_same_bar=False で再評価してしまい、レビュー修正 1 で
            # 塞いだ「同一バー TP の誤確定」が再発する。
            try:
                bar = self._fresh_bar(row["pair"], now)
                if bar is None:
                    continue
                price = check_limit_fill(row, bar,
                                         self._spread_for(row["pair"]))
                if price is None:
                    continue
                transitions.transition(
                    self.conn, row["id"], S.PROTECTION_PENDING, now,
                    avg_fill_price=price, filled_quantity=row["quantity"],
                    remaining_quantity=0.0, filled_at=now.isoformat())
                transitions.transition(self.conn, row["id"], S.OPEN, now)
                # filled_ids への追加は OPEN 遷移の**直後・activity より前**。
                # activity.write は I/O (ENOSPC 等で OSError になり得る) で、
                # ここで例外が出ると「DB は OPEN なのに filled_ids に無い」
                # 注文が生まれ、_process_exits が entry_same_bar=False で
                # 再評価して同一バー TP を誤確定させる (再レビュー N1 で実測)。
                filled_ids.add(row["id"])
                self.activity.write(Category.TRADE, "limit_filled",
                                    f"{row['pair']} @{price}",
                                    ref_id=str(row["id"]))
                # 同一バーで SL/TP に到達し得る → 保守則で即時判定
                filled = orders.get(self.conn, row["id"])
                self._check_one_exit(filled, bar, entry_same_bar=True)
            except Exception as e:  # noqa: BLE001 — 資金保護を止めない
                text = safe_error_text(e)
                self.activity.write(Category.TRADE, "limit_fill_error",
                                    f"#{row['id']} {row['pair']}: {text} "
                                    "— 次 tick 再試行", ref_id=str(row["id"]))
                _log.warning("limit fill failed #%s: %s", row["id"], text)
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
            # codex C-I2: 1 注文分の例外 (bars_fn / spec_fn / broker の
            # 想定外例外) が、**残りの注文の SL/TP 監視を殺してはならない**。
            # 例外時はその注文をスキップして記録するだけ — **架空価格で
            # クローズはしない** (価格が取れていないのだから、閉じるより
            # 次 tick に賭けるほうが安全)。
            try:
                bar = self._fresh_bar(row["pair"], now)
                if bar is not None:
                    self._check_one_exit(row, bar, entry_same_bar=False)
            except Exception as e:  # noqa: BLE001 — 他注文の保護を止めない
                text = safe_error_text(e)
                # N2 第二層 (defense in depth): ActivityLog.write は送出しない
                # 契約 (一次修正) だが、ここは資金保護の最重要走査なので、
                # 注入された activity double が契約を破って例外を投げても
                # 走査 (残りの注文の SL/TP 監視) は止めない。
                try:
                    self.activity.write(
                        Category.TRADE, "exit_check_error",
                        f"#{row['id']} {row['pair']}: {text} — 次 tick 再試行",
                        ref_id=str(row["id"]))
                except Exception as write_err:  # noqa: BLE001
                    _log.warning(
                        "activity write failed (exit_check_error) #%s: %s",
                        row["id"], safe_error_text(write_err))
                _log.warning("exit check failed #%s: %s", row["id"], text)
        # レビュー修正 5: 今 tick で見たバーを処理済みとして記録する走査対象
        # は、その時点の orders テーブルの状態 (PENDING_FILL/OPEN/CLOSED/
        # CANCELLED が存在する pair) ではなく、config で宣言されている全
        # ペアにする。元の実装は「この tick でこれら 4 状態のいずれの注文も
        # 無い pair」のバーを一切マーキングせず、状態集合のドリフト (期限
        # 切れのみ・unknown のみ等) に依存してしまっていた。マーキングは
        # account の有無に関わらず (_process_limit_fills が呼ばれなかった
        # tick でも) 必ず行う。
        # codex C-I2: マーキングも同じ bars_fn を呼ぶ。ここで例外が漏れると
        # tick の残り (毎時 Mission) まで飛ぶうえ、_process_exits の末尾
        # という「資金保護の直後」で落ちるため見た目が紛らわしい。ペア単位で
        # 隔離する (マーキング漏れは同一バーの再処理を許すだけで、
        # 保守側の check_exit が二重クローズを作ることはない)。
        for pair in self.settings.pairs:
            try:
                bar = self.bars_fn(pair)
            except Exception as e:  # noqa: BLE001
                _log.warning("processed-bar marking failed for %s: %s", pair,
                             safe_error_text(e))
                continue
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

"""BacktestRunner — in-memory 再生・synthetic Mission・先読み禁止 (プラン 6 Task 7)。

実運用と同一コード (Scheduler / Executor / PaperBroker / missions /
TradeIntent.from_llm_dict / market_hours.is_market_open) を、in-memory の
sqlite 接続と ``ReplayClock``/``BarFeed`` (Task 6) で駆動する。

mode=learning / autopilot=off での再生 (実運用 Phase 2 と同一条件での再生が
忠実 — レビュー裁定 codex I5)。trading モードでの再生・約定監視は Phase 3
のスコープ。

先読み禁止 (§6) の配線契約 (レビュー裁定 2026-08-01, Task 6 fix round 1 の
設計変更を反映 — brief 本文の ``bar_at(current_ts)`` 配線は使わない):

- ``bars_fn``/``quote_fn`` は ``BarFeed.latest_completed_1m(current_ts)`` —
  ``bar_at(now)`` は ``[now, now+1m)`` のまだ形成中のバーを返すため、判断・
  約定材料に使うと先読みになる。
- バケット集約 (評価 timeframe の確定バー) だけは ``BarFeed.bar_at`` を
  直接読む — バケット終端の tick では、そのバケットに属する全ての 1 分足が
  既に完成しているため先読みではない。

equity の二重の意味 (fix round 1 F1 — codex Critical の裁定): この関数が
返す ``BacktestResult.equity_curve`` は **実現損益ベース**
(``PaperBroker.equity()`` の契約どおり、含み損益を含まない)。一方、
``Scheduler._mark_to_market`` が ``account_snapshots`` に書き込む equity
(kill switch のドローダウン判定・risk gate の総リスク評価が読む値) は
**実運用と同じく含み損益込みの mark-to-market**である。両者は別物であり、
BacktestRunner はこの違いを実運用のまま再現する (これは欠陥ではなく
実運用の保護設計の忠実な再現 — コントローラ裁定 2026-08-01)。Task 8 の
成績集計は ``equity_curve`` (実現損益ベース) を使うため、含み損益込みの
評価が必要な場合は別途 mark-to-market snapshot を参照すること。
"""
from __future__ import annotations

import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agentic_fx.backtest.replay import BarFeed, ReplayClock, quote_from_bar
from agentic_fx.backtest.timeframes import ceil_to_bucket, floor_to_bucket
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.activity import Category
from agentic_fx.config import Settings
from agentic_fx.core import market_hours
from agentic_fx.core.accounting import drawdown_pct, record_snapshot
from agentic_fx.core.contracts import Bar, ConversionRate, Origin, TradeIntent
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.store import missions, snapshots as snapshot_store
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

# strategy plugin アダプタはこの型に合わせる (プラン 7)。引数は確定した
# 評価 timeframe バー、返り値は LLM 出力と同形の dict / None = 提案なし。
IntentSource = Callable[[Bar], dict | None]


class _KillSwitchCompensationError(RuntimeError):
    pass

_TF_RE = re.compile(r"^(\d+)(m|h)$")
# バケット境界の錨。実運用の datafeed.bars.BAR_ANCHOR="epoch" と揃える
# (start 相対だと start が tf 格子に乗らない再生で境界がずれる)。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _RecordingActivity:
    """Backtest 専用の in-memory ActivityLog 代替。

    実物と同じ ``write`` 境界だけを提供し、ファイル出力はしない。時刻は
    ``datetime.now()`` ではなく replay clock から採るため、golden を決定的に
    保てる。
    """

    def __init__(self, clock: ReplayClock) -> None:
        self._clock = clock
        self.entries: list[dict] = []

    def write(self, category, event: str, summary: str,
              ref_id: str | None = None) -> None:
        """Mirror ``ActivityLog.write`` and never interrupt replay."""
        try:
            self.entries.append({
                "ts": self._clock.now().astimezone(timezone.utc).isoformat(),
                "category": getattr(category, "value", str(category)),
                "kind": event,
                "text": summary,
                "ref_id": ref_id,
            })
        except Exception:
            # Activity is observability only; scheduler protection must proceed.
            return


class _RecordingStateStore(StateStore):
    """ラッチ遷移を replay の観測面へ追加する StateStore。"""

    def __init__(self, path: Path, *, clock: ReplayClock,
                 snapshot_id_fn: Callable[[], int | None] | None = None) -> None:
        super().__init__(path)
        self._clock = clock
        self._snapshot_id_fn = snapshot_id_fn
        self.kill_switch_transitions: list[dict] = []

    def update(self, *, transition_snapshot_id=None, publish=True, **changes):
        before = self.load().kill_switch_latched
        snapshot_id = transition_snapshot_id
        is_transition = ("kill_switch_latched" in changes and
                         changes["kill_switch_latched"] != before)
        if (is_transition and snapshot_id is None and
                self._snapshot_id_fn is not None):
            snapshot_id = self._snapshot_id_fn()
        result = super().update(**changes)
        if publish and result.kill_switch_latched != before:
            self.kill_switch_transitions.append({
                "ts": self._clock.now().astimezone(timezone.utc).isoformat(),
                "kind": "latched" if result.kill_switch_latched else "released",
                "reason": "state_store kill_switch_latched transition",
                "snapshot_id": snapshot_id,
            })
        return result

    def publish_released(self, *, ts: datetime, snapshot_id: int,
                         reason: str) -> None:
        self.kill_switch_transitions.append({
            "ts": ts.astimezone(timezone.utc).isoformat(), "kind": "released",
            "reason": reason, "snapshot_id": snapshot_id})


@dataclass
class BacktestResult:
    orders: list[dict]
    equity_curve: list[tuple[str, float]]
    start: datetime
    end: datetime
    source: str
    fallback_spread_used: bool
    snapshots: list[dict] = field(default_factory=list)
    kill_switch_events: list[dict] = field(default_factory=list)
    kill_switch_latches: int = 0
    first_decision_at: datetime | None = None


def parse_timeframe(tf: str) -> timedelta:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unsupported eval_timeframe: {tf!r}")
    n, unit = int(m.group(1)), m.group(2)
    if n == 0:
        # fix round 1 F7: "0m"/"0h" は正規表現には通るが tf=timedelta(0) と
        # なり、後段の `(now - _EPOCH) % tf` が ZeroDivisionError になる。
        raise ValueError(f"eval_timeframe must be > 0: {tf!r}")
    return timedelta(hours=n) if unit == "h" else timedelta(minutes=n)


def _require_base_grid(dt: datetime, label: str, width: timedelta) -> None:
    """``ReplayClock``/``BarFeed`` と同じ正時格子契約 (second==microsecond==0)
    を ``end`` にも適用する (fix round 1 F3 — codex Important)。格子外の
    ``end`` は宣言した ``[start, end)`` を最大 1 分近く超過して処理して
    しまう (``while now < end`` が格子未満の余りぶん余計に 1 回多く回る)。
    """
    if dt.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    dt_utc = dt.astimezone(timezone.utc)
    if (dt_utc - _EPOCH) % width:
        raise ValueError(f"{label} must be on minute boundary / base interval grid")


def _aggregate_bucket(feed: BarFeed, symbol: str, interval: str,
                      bucket_start: datetime, tf: timedelta) -> Bar | None:
    """[bucket_start, bucket_start+tf) の 1m バーを集約する。

    レビュー裁定 codex I3: bucket は UTC 半開区間、部分欠損はある分だけで
    集約 (実運用の get_ohlcv も欠損込みで返すため忠実)、全欠損はスキップ。
    このループは全て「その時点で完成済み」の 1 分足を読むだけ (バケット
    終端の tick で呼ばれる = バケット内の全 1 分足は既に過去) なので
    ``feed.bar_at`` の直接使用は先読みにならない。
    """
    bars: list[Bar] = []
    t = bucket_start
    while t < bucket_start + tf:
        bar = feed.bar_at(t)
        if bar is not None:
            bars.append(bar)
        t += feed._width
    if not bars:
        return None
    return Bar(
        symbol=symbol, interval=interval, ts=bucket_start,
        open=bars[0].open, high=max(b.high for b in bars),
        low=min(b.low for b in bars), close=bars[-1].close,
        volume=sum(b.volume for b in bars))


def _kill_switch_event(conn: sqlite3.Connection, entry: dict, kind: str,
                       activity_entries: list[dict] | None = None) -> dict:
    """kill_switch_events の 1 要素を組み立てる (段階 1 レビュー是正 6c を
    テスト可能にするための module-level 抽出 — 挙動は不変)。

    ``drawdown_pct`` は ``entry["ts"]`` 以前の最新 ``account_snapshots``
    (``ORDER BY ts DESC, id DESC LIMIT 1`` — 同時刻の複数 snapshot は最大
    id を選ぶ) から ``(hwm − equity)/hwm×100`` を計算する。``hwm <= 0``
    (未初期化・境界) は ``None`` (計算不能)。
    """
    row = None
    if entry.get("snapshot_id") is not None:
        row = conn.execute("SELECT equity, hwm FROM account_snapshots WHERE id=?",
                           (entry["snapshot_id"],)).fetchone()
    elif "snapshot_id" not in entry:  # legacy direct helper callers
        row = conn.execute(
            "SELECT equity, hwm FROM account_snapshots WHERE ts <= ? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (entry["ts"],)).fetchone()
    drawdown = None
    if row is not None and row["hwm"] > 0:
        drawdown = max(0.0, (row["hwm"] - row["equity"])
                       / row["hwm"] * 100.0)
    reason = entry.get("reason", "state_store kill_switch_latched transition")
    if kind == "latched" and activity_entries is not None:
        match = next((a for a in activity_entries
                      if a["ts"] == entry["ts"] and
                      a["kind"] == "kill_switch_latched"), None)
        if match is not None:
            reason = match["text"]
    return {"ts": entry["ts"], "kind": kind, "reason": reason,
            "drawdown_pct": drawdown}


def _auto_release_kill_switch(conn: sqlite3.Connection, *,
                              state: _RecordingStateStore,
                              activity: _RecordingActivity, now: datetime,
                              mtm: dict, latched_at: datetime) -> bool:
    """Replay-local kill-switch release with DB rollback/state compensation."""
    if now < datetime.fromisoformat(mtm["ts"]):
        activity.write(Category.SYSTEM, "replay_kill_switch_release_deferred",
                       "MTM snapshot timestamp is after release tick")
        return False
    hwm_before = mtm["hwm"]
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO account_snapshots "
            "(ts,balance,equity,hwm,cashflow,source) VALUES (?,?,?,?,?,?)",
            (now.astimezone(timezone.utc).isoformat(), mtm["balance"],
             mtm["equity"], mtm["equity"], 0.0, "replay_ks_rebase"))
        rebase_id = cur.lastrowid
        rebased = snapshot_store.latest(conn)
        valid = (rebased is not None and rebased["id"] == rebase_id and
                 rebased["ts"] == now.astimezone(timezone.utc).isoformat() and
                 rebased["source"] == "replay_ks_rebase" and
                 rebased["equity"] == rebased["hwm"] and
                 drawdown_pct(rebased["equity"], rebased["hwm"]) == 0)
        if not valid:
            conn.rollback()
            activity.write(Category.SYSTEM,
                           "replay_kill_switch_release_deferred",
                           "rebase snapshot postcondition failed")
            return False
        state.update(kill_switch_latched=False,
                     transition_snapshot_id=rebase_id, publish=False)
        try:
            conn.commit()
        except Exception:
            conn.rollback()
            try:
                state.update(kill_switch_latched=True, publish=False)
            except Exception as exc:
                raise _KillSwitchCompensationError(
                    "kill-switch state compensation failed") from exc
            activity.write(Category.SYSTEM, "replay_kill_switch_release_failed",
                           "commit failed; state compensated")
            return False
        state.publish_released(ts=now, snapshot_id=rebase_id,
                               reason="replay_kill_switch_auto_release")
        activity.write(
            Category.SYSTEM, "replay_kill_switch_auto_release",
            f"latched_at={latched_at.isoformat()} released_at={now.isoformat()} "
            f"hwm_before={hwm_before} hwm_after={rebased['hwm']} "
            f"rebase_id={rebase_id}")
        return True
    except _KillSwitchCompensationError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        activity.write(Category.SYSTEM, "replay_kill_switch_release_deferred",
                       f"release operation failed: {type(exc).__name__}")
        return False
    finally:
        assert conn.in_transaction is False


def _release_is_due(*, now: datetime, latched_at: datetime, mtm: dict | None,
                    before_id: int) -> bool:
    return bool(now >= market_hours.next_rollover(latched_at)
                and market_hours.is_market_open(now) and mtm is not None
                and mtm["id"] > before_id and mtm["ts"] == now.isoformat()
                and mtm["source"] == "paper")


def _executor_transition_snapshot_id(latest: dict | None,
                                     now: datetime) -> int | None:
    if (latest is None or latest["ts"] != now.isoformat()
            or latest["source"] != "paper"):
        return None
    return latest["id"]


def run_replay(settings: Settings, *, symbol: str, dataset: HistoryDataset,
               start: datetime, end: datetime,
               intent_source: IntentSource,
               eval_timeframe: str = "1h",
               history_conn: sqlite3.Connection) -> BacktestResult:
    """``[start, end)`` を 1 分刻みで再生し、``BacktestResult`` を返す。

    実運用のコア (Scheduler/Executor/PaperBroker/missions/TradeIntent) を
    そのまま使い、in-memory の sqlite 接続と一時ファイルの ``StateStore``
    で駆動する — 実 DB (``data/``) には一切触れない。``history_conn`` は
    OHLCV 履歴の読み取り専用接続で、実行時状態を持つ in-memory 接続とは
    別物 (``BarFeed`` にのみ渡す)。
    """
    tf = parse_timeframe(eval_timeframe)
    if tf < dataset.width or tf % dataset.width:
        raise ValueError("eval_timeframe must be an integer multiple of base_interval")
    _require_base_grid(start, "start", dataset.width)
    _require_base_grid(end, "end", dataset.width)
    first_decision_at = ceil_to_bucket(start, eval_timeframe) + tf

    conn = connect(Path(":memory:"))
    init_db(conn)
    clock = ReplayClock(first_decision_at, dataset.width)
    def transition_snapshot_id() -> int | None:
        return _executor_transition_snapshot_id(snapshot_store.latest(conn),
                                                clock.now())

    state = _RecordingStateStore(Path(tempfile.mkdtemp()) / "state.json",
                                 clock=clock,
                                 snapshot_id_fn=transition_snapshot_id)

    # レビュー裁定 sonnet C1: PaperBroker.equity() は settings.paper.
    # starting_balance 基準のため、backtest.initial_balance と二重基準に
    # ならないよう bt_settings を作り、broker/executor/scheduler には
    # これを渡す。record_snapshot の初期投入と一致させる (kill switch が
    # 初回 tick で誤ラッチしないため)。
    initial_balance = settings.backtest.initial_balance
    bt_settings = settings.model_copy(update={
        "paper": settings.paper.model_copy(
            update={"starting_balance": initial_balance})})
    record_snapshot(conn, now=start, balance=initial_balance,
                    equity=initial_balance)

    preload_start = floor_to_bucket(start, eval_timeframe) - tf * 200
    feed = BarFeed(history_conn, symbol, dataset=dataset, start=preload_start, end=end)
    fallback_spread_used = False
    current_ts = start

    def _spread(bar_ts: datetime) -> float:
        nonlocal fallback_spread_used
        sp = feed.spread_at(bar_ts)
        if sp is not None:
            return sp
        fallback_spread_used = True
        rule = bt_settings.risk.pair_rules[symbol]
        return rule.assumed_spread_pips * _SPECS[symbol].pip_size

    def bars_fn(pair: str) -> Bar | None:
        if pair != symbol:
            return None
        return feed.latest_completed(current_ts)

    def quote_fn(pair: str):
        # bar が None なら例外を投げてよい (executor 側の既存例外処理に
        # 任せる — 無 quote で発注が通る方が危険。レビュー裁定 上書き A)。
        bar = feed.latest_completed(current_ts)
        if bar is None:
            raise ValueError(
                f"no completed {dataset.base_interval} bar for {pair} at "
                f"{current_ts.isoformat()} (no-lookahead quote source)")
        return quote_from_bar(bar, _spread(bar.ts))

    def spec_fn(pair: str):
        return _SPECS[pair]  # 表に無い pair は KeyError で fail closed

    def rate_fn(ccy: str, account_ccy: str, now: datetime, *,
                deadline_check: Callable[[str], None] | None = None
                ) -> ConversionRate:
        # `Executor` の `RateFn` 契約 (keyword-only `deadline_check` を受理
        # 必須) に適合させる。backtest は `handle_intent` 直呼びで gather を
        # 通らないため実際には常に `None` で、挙動は従前と完全に同一 —
        # 受けるだけで使わない (束B レビュー: 契約の宣言/実態一致)。
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (now,))
        spec = _SPECS[symbol]
        if ccy == spec.base_currency and account_ccy == spec.quote_currency:
            bar = feed.latest_completed(current_ts)
            if bar is None:
                raise ValueError(
                    f"no completed {dataset.base_interval} bar for rate conversion at "
                    f"{current_ts.isoformat()}")
            return ConversionRate(bar.close, ccy, account_ccy, (bar.ts,))
        raise ValueError(
            f"マルチ通貨換算は未対応 (single-symbol replay): "
            f"{ccy}->{account_ccy}")

    broker = PaperBroker(conn, bt_settings, clock)
    activity = _RecordingActivity(clock)
    executor = Executor(
        conn=conn, broker=broker, settings=bt_settings, state_store=state,
        activity=activity, notifier=Notifier(False, None), clock=clock,
        quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)
    scheduler = Scheduler(
        conn=conn, executor=executor, settings=bt_settings, state_store=state,
        activity=activity, bars_fn=bars_fn,
        on_trade_mission=lambda reason: None,
        on_news_cycle=lambda: None, on_econ_cycle=lambda: None,
        bar_freshness=dataset.width + timedelta(minutes=1))

    equity_curve: list[tuple[str, float]] = [
        (start.isoformat(), initial_balance)]
    pending_proposal: dict | None = None
    now = first_decision_at
    while now < end:
        current_ts = now
        before = snapshot_store.latest(conn)
        before_id = before["id"] if before is not None else 0
        scheduler.tick(now)            # 市場クローズ判定は tick 内部 — 無条件に毎分呼ぶ
        if state.load().kill_switch_latched:
            latched = next((t for t in reversed(state.kill_switch_transitions)
                            if t["kind"] == "latched"), None)
            if latched is not None:
                latched_at = datetime.fromisoformat(latched["ts"])
                mtm = snapshot_store.latest(conn)
                if _release_is_due(now=now, latched_at=latched_at, mtm=mtm,
                                   before_id=before_id):
                    _auto_release_kill_switch(
                        conn, state=state, activity=activity, now=now,
                        mtm=mtm, latched_at=latched_at)
        assert conn.in_transaction is False
        if pending_proposal is not None:
            # fix round 1 F2 (codex Important + sonnet Important): 評価時は
            # 市場オープンでも、1 tick 遅れの執行時にクローズしている場合が
            # ある (例: 金曜クローズ直前バケットの提案)。執行直前に再確認し、
            # クローズ中なら発注せず破棄する (週明けの執行は stale で不可)。
            if (market_hours.is_market_open(now)
                    and feed.latest_completed(now) is not None):
                mid = missions.start(conn, "trade", "backtest",
                                     "intent-source", now)
                missions.finish(conn, mid, "completed", pending_proposal, [],
                                now)
                intent = TradeIntent.from_llm_dict(pending_proposal,
                                                   origin=Origin.SCHEDULER)
                executor.handle_intent(intent, mid)
            elif market_hours.is_market_open(now):
                activity.write(
                    Category.TRADE, "proposal_dropped_no_bar",
                    f"proposal dropped: no completed bar at {now.isoformat()}")
            pending_proposal = None
        # バケット境界の錨は UTC epoch (実運用の datafeed.bars.resample /
        # BAR_ANCHOR="epoch" と同じ規律) — `start` 相対にすると、start が
        # tf の格子に乗っていない再生 (例: start=12:30, tf="1h") で
        # [12:30,13:30) のような実運用に存在しない境界のバケットを作って
        # しまう。epoch 錨なら常に実運用と同じ [12:00,13:00) 等の境界になる。
        if (now - _EPOCH) % tf == timedelta(0):
            bucket_start = now - tf
            closed_bar = _aggregate_bucket(feed, symbol, eval_timeframe,
                                           bucket_start, tf)
            if closed_bar is not None and market_hours.is_market_open(now):
                pending_proposal = intent_source(closed_bar)
        # fix round 1 F7 (sonnet M1): 先頭の seed 点 (start, initial_balance)
        # と初回ループの tail 追記が同一 ts になり二重記録されるため、その
        # 場合だけ tail 追記をスキップする (seed 点はそのまま維持)。
        # 段階 3 是正: 先頭足規則 (A2) 導入後は first_decision_at != start
        # が通常になった。旧比較 (`now != first_decision_at`) のままだと
        # ループの最初の tick (now == first_decision_at) の equity 点が
        # 常に (start と重複していないのに) 誤って欠落する — 比較対象を
        # 重複が実際に起き得る `start` に修正する (収束テストで発見)。
        if now != start:
            equity_curve.append((now.isoformat(), broker.equity()[1]))
        now = clock.advance()

    order_rows = [dict(r) for r in
                 conn.execute("SELECT * FROM orders ORDER BY id").fetchall()]
    snapshots = [dict(r) for r in conn.execute(
        "SELECT * FROM account_snapshots ORDER BY ts, id").fetchall()]

    kill_switch_events = [
        _kill_switch_event(conn, transition, transition["kind"], activity.entries)
        for transition in state.kill_switch_transitions]
    # 観測面の順序は時刻で固定する (activity 由来と StateStore 由来が
    # 別 list から合流するため、合流順に依存させない)。
    kill_switch_events.sort(key=lambda e: (e["ts"], e["kind"]))
    return BacktestResult(
        orders=order_rows, equity_curve=equity_curve, start=start, end=end,
        source=dataset.source, fallback_spread_used=fallback_spread_used,
        snapshots=snapshots, kill_switch_events=kill_switch_events,
        kill_switch_latches=sum(
            t["kind"] == "latched" for t in state.kill_switch_transitions),
        first_decision_at=first_decision_at)

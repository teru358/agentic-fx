"""履歴検査の結果・証跡を作る test 用の共通 helper。

本物と同じ結果型 (`HistoryHealth`) と証跡 (`history_evidence.build_evidence`) を返す。
手書きの辞書で証跡を作らない (本物の検証を通る形にするため)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from agentic_fx.backtest import history_evidence as ev
from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.holdout import _normalize_now, in_sample_until

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def ready_health(*, dataset: HistoryDataset, pairs, now: datetime, holdout_months: int = 3,
                 no_bars_path: Path | None = None,
                 verdict: str = hh.HISTORY_READY) -> hh.HistoryHealth:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not isinstance(holdout_months, int):
        holdout_months = 3
    start = in_sample_until(now, holdout_months, base_interval=dataset.base_interval)
    end = _normalize_now(now, dataset.base_interval)
    no_bars = hh.load_no_bars(no_bars_path or ev.no_bars_path(Path("/nonexistent-root")))
    pair_list = list(dict.fromkeys(pairs))
    latest = end - dataset.width
    if verdict == hh.HISTORY_READY:
        health_pairs = tuple(
            hh.PairHealth(p, (), latest, latest, now - end, timedelta(0), 1000, 1000,
                          Fraction(1), (), (end.date(), 0)) for p in pair_list)
        reasons: tuple[str, ...] = ()
    else:
        health_pairs = tuple(
            hh.PairHealth(p, (verdict,), None, None, None, None, 0, 0, None, (), None, "fake")
            for p in pair_list)
        reasons = (verdict,)
    applied = ev.scope_no_bars(no_bars.entries, dataset=dataset, pairs=pair_list,
                               start=start, end=end)
    return hh.HistoryHealth(verdict, reasons, health_pairs, now, dataset, start, end,
                            no_bars.digest, applied)


def ready_evidence(*, source: str = "dukascopy", base_interval: str = "1m", pairs=("USDJPY",),
                   now: datetime = NOW, holdout_months: int = 3,
                   no_bars_path: Path | None = None) -> dict:
    """承認 payload の `history_guard` に載せる、本物の検証を通る証跡。"""
    health = ready_health(dataset=HistoryDataset(source, base_interval), pairs=pairs, now=now,
                          holdout_months=holdout_months, no_bars_path=no_bars_path)
    return ev.build_evidence(health)


# ---- 実 sqlite に評価用履歴を入れる (本物の検査を通す入口 test 用) -------------------

def _floor5(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def hole_day(now: datetime) -> tuple[datetime, datetime]:
    """評価区間の中ほどにある平日の 03:00-06:00 UTC (開場中、5m で 36 本 > 60 分)。"""
    day = (now - timedelta(days=12)).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day + timedelta(hours=3), day + timedelta(hours=6)


def fill_history(conn, *, source: str, pairs, now: datetime, scenario: str = "normal",
                 interval: str = "5m") -> None:
    """``now`` の時点で ``scenario`` の形になる履歴を pair ごとに入れる。

    normal = 直近の確定足まで揃う / stale = 最終足が 72 時間前 / gap = 直近まで揃うが
    1 日に 60 分を超える欠損がある。足は FX 暦の開場中だけ。"""
    from agentic_fx.core.market_hours import is_market_open
    step = timedelta(minutes=int(interval[:-1]))
    last = _floor5(now) if scenario != "stale" else _floor5(now - timedelta(hours=72))
    hole = hole_day(now) if scenario == "gap" else None
    rows = []
    for symbol in pairs:
        t = _floor5(now - timedelta(days=45))
        while t < last:
            if is_market_open(t) and not (hole and hole[0] <= t < hole[1]):
                rows.append((symbol, interval, t.isoformat(), 148.0, 148.2, 147.9, 148.1,
                             1.0, source))
            t += step
    conn.executemany(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
        "close, volume, source, spread) VALUES (?,?,?,?,?,?,?,?,?,NULL)", rows)
    conn.commit()


def write_no_bars(root: Path, *, source: str, symbol: str, day, interval: str = "5m") -> Path:
    """root 基準の無足日一覧 (config/history-no-bars.yaml) に 1 日を書く。"""
    path = ev.no_bars_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "entries:\n"
        f"  - source: {source}\n    base_interval: {interval}\n    symbol: {symbol}\n"
        f"    date_utc: \"{day.isoformat()}\"\n    reason: \"test closure\"\n"
        "    approved_by: tester\n    approved_at: \"2026-01-01T00:00:00+00:00\"\n",
        encoding="utf-8")
    return path

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

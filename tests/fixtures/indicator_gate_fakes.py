"""indicator gate の fixture 検査を、sandbox を起動せずに通すための差し替え。

`indicator_probe` の `open_session` / `run_contract` / `run_probe` を、plugin 名から
出力を引く純 Python の関数に置き換える。出力の計算は本番と同じ入力 (正規 fixture の
末尾 `max_bars` 本 / 4096 本、4 通りの倍率) に対して行うので、分類・比較・cache は
本物のコードを通る。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from agentic_fx.plugin import indicator_probe as probe
from agentic_fx.plugin.loader import PluginMeta

Outputs = dict


def ratio(df: pd.DataFrame) -> Outputs:
    """価格の単位に依らない (invariant)。"""
    return {"value": df["close"].pct_change()}


def affine_of_ratio(df: pd.DataFrame) -> Outputs:
    """`ratio` の単調変換 (exact ではないが順位は同じ)。"""
    return {"value": 2.0 * df["close"].pct_change() + 1.0}


def price_diff(df: pd.DataFrame) -> Outputs:
    """価格に比例する (proportional)。"""
    return {"value": df["close"].diff()}


def absolute_threshold(df: pd.DataFrame) -> Outputs:
    """価格の単位に依存する (neither)。"""
    return {"value": ((df["close"] - df["open"]) > 0.05).astype(float)
            + 0.001 * df["close"].pct_change().fillna(0.0)}


def constant(df: pd.DataFrame) -> Outputs:
    return {"value": pd.Series(1.0, index=df.index)}


def constant_in_contract_only(df: pd.DataFrame) -> Outputs:
    """契約 run (短い窓) では定数、解析 probe (長い窓) では動く。"""
    if len(df) <= 200:
        return constant(df)
    return ratio(df)


def sparse(df: pd.DataFrame) -> Outputs:
    """有限な点が 5 本だけ (疎な列)。"""
    values = pd.Series(float("nan"), index=df.index)
    values.iloc[-5:] = [1.0, 2.0, 3.0, 4.0, 5.0]
    return {"value": values}


def all_nan(df: pd.DataFrame) -> Outputs:
    return {"value": pd.Series(np.nan, index=df.index)}


def meta_for(name: str, directory: Path, *, content_hash: str,
             max_bars: int = 200, outputs: tuple[str, ...] = ("value",),
             kind: str = "indicator") -> PluginMeta:
    return PluginMeta(
        name=name, kind=kind, path=directory, params={}, timeframe="1h",
        pairs=(), max_bars=max_bars, content_hash=content_hash,
        outputs=outputs)


class FakeProbe:
    """`outputs_by_name[name](df)` を計算する代わりの sandbox。

    `events` は呼び出しの順序 ("contract:<name>" / "probe:<name>")。`env_error`
    を立てると session を開く段で環境起因の失敗にする。`fail_names` の plugin は
    probe で失敗する (配備済みの cache 生成だけが失敗する場面の再現)。"""

    def __init__(self, outputs_by_name: dict[str, Callable[[pd.DataFrame], Outputs]],
                 *, contract_failure: str | None = None,
                 probe_failure: str | None = None, env_error: bool = False,
                 fail_names: frozenset[str] = frozenset()) -> None:
        self.outputs_by_name = outputs_by_name
        self.contract_failure = contract_failure
        self.probe_failure = probe_failure
        self.env_error = env_error
        self.fail_names = fail_names
        self.events: list[str] = []

    def _series(self, name: str, df: pd.DataFrame) -> probe.Outputs:
        return {k: v for k, v in self.outputs_by_name[name](df).items()}

    def install(self, monkeypatch) -> "FakeProbe":
        fake = self

        @contextmanager
        def open_session(meta, settings):
            if fake.env_error:
                raise probe.IndicatorEnvironmentError("sandbox_unavailable")
            fake.events.append(f"open:{meta.name}")
            yield meta

        def run_contract(session, candidate, bars):
            fake.events.append(f"contract:{candidate.name}")
            if fake.contract_failure:
                return probe.ContractResult(failure=fake.contract_failure)
            df = bars.tail(candidate.max_bars).copy()
            return probe.ContractResult(outputs=fake._series(candidate.name, df))

        def run_probe(session, candidate, bars, *, wall_clock_max_sec=60.0, **_):
            fake.events.append(f"probe:{candidate.name}")
            if fake.probe_failure or candidate.name in fake.fail_names:
                return probe.ProbeResult(
                    failure=fake.probe_failure or "probe_exception")
            tail = bars.tail(probe.PROBE_BARS)
            runs = {label: fake._series(candidate.name,
                                        probe._scaled(tail, price, volume))
                    for label, price, volume in probe.PROBE_RUNS}
            nan_runs = tuple(label for label, outs in runs.items()
                             if probe.outputs_all_nan(outs))
            return probe.ProbeResult(runs=runs, all_nan_runs=nan_runs)

        monkeypatch.setattr(probe, "open_session", open_session)
        monkeypatch.setattr(probe, "run_contract", run_contract)
        monkeypatch.setattr(probe, "run_probe", run_probe)
        return self

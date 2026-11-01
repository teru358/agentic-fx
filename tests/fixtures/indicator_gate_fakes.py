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


def cubic_of_ratio(df: pd.DataFrame) -> Outputs:
    """`ratio` の厳密な単調変換 (水準の順位は同じ、差分の順位は少しずれる)。"""
    base = df["close"].pct_change()
    return {"value": base + 0.1 * base ** 3 / float(base.std()) ** 2}


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


def constant_in_probe_only(df: pd.DataFrame) -> Outputs:
    """契約 run (短い窓) では動き、解析 probe (長い窓) では定数。"""
    if len(df) <= 200:
        return ratio(df)
    return constant(df)


def sparse_in_contract_only(df: pd.DataFrame) -> Outputs:
    if len(df) <= 200:
        return sparse(df)
    return ratio(df)


def sparse_in_probe_only(df: pd.DataFrame) -> Outputs:
    if len(df) <= 200:
        return ratio(df)
    return sparse(df)


def noise(df: pd.DataFrame) -> Outputs:
    """ratio と無関係な系列 (index にだけ依存、価格の単位に依らない)。"""
    rng = np.random.default_rng(7)
    return {"value": pd.Series(rng.normal(size=len(df)), index=df.index)}


def negated_ratio(df: pd.DataFrame) -> Outputs:
    return {"value": -df["close"].pct_change()}


def ratio_with_wobble(df: pd.DataFrame) -> Outputs:
    """ratio に弱い周期の揺れを足した系列 (差分の Pearson が高く、順位は一致しない)。"""
    base = df["close"].pct_change()
    wobble = 0.3 * float(base.std()) * np.sin(np.arange(len(df)))
    return {"value": base + wobble}


def two_columns(df: pd.DataFrame) -> Outputs:
    return {"value": df["close"].pct_change(), "extra": df["close"].diff()}


def one_shared_column(df: pd.DataFrame) -> Outputs:
    """`two_columns` の `value` だけが一致し、他の列は固有。"""
    return {"value": df["close"].pct_change(), "own": noise(df)["value"]}


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
    を立てると session を開く段で環境起因の失敗にする。`fail_names` の plugin (名前か content hash) は
    probe で失敗する (配備済みの cache 生成だけが失敗する場面の再現)。"""

    def __init__(self, outputs_by_name: dict[object, Callable[[pd.DataFrame], Outputs]],
                 *, contract_failure: str | None = None,
                 probe_failure: str | None = None, env_error: bool = False,
                 fail_names: frozenset[str] = frozenset()) -> None:
        self.outputs_by_name = outputs_by_name
        self.contract_failure = contract_failure
        self.probe_failure = probe_failure
        self.env_error = env_error
        self.fail_names = fail_names
        self.events: list[str] = []

    def _series(self, candidate: PluginMeta, df: pd.DataFrame) -> probe.Outputs:
        """`(name, content_hash)` の key があればそれを、無ければ name の key を引く
        (同名の旧版と新版で別の式を返したいとき)。"""
        fn = self.outputs_by_name.get(
            (candidate.name, candidate.content_hash),
            self.outputs_by_name.get(candidate.name))
        return {k: v for k, v in fn(df).items()}

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
            return probe.ContractResult(outputs=fake._series(candidate, df))

        def run_probe(session, candidate, bars, *, wall_clock_max_sec=60.0, **_):
            fake.events.append(f"probe:{candidate.name}")
            if (fake.probe_failure or candidate.name in fake.fail_names
                    or candidate.content_hash in fake.fail_names):
                return probe.ProbeResult(
                    failure=fake.probe_failure or "probe_exception")
            tail = bars.tail(probe.PROBE_BARS)
            runs = {label: fake._series(candidate,
                                        probe._scaled(tail, price, volume))
                    for label, price, volume in probe.PROBE_RUNS}
            nan_runs = tuple(label for label, outs in runs.items()
                             if probe.outputs_all_nan(outs))
            return probe.ProbeResult(runs=runs, all_nan_runs=nan_runs)

        monkeypatch.setattr(probe, "open_session", open_session)
        monkeypatch.setattr(probe, "run_contract", run_contract)
        monkeypatch.setattr(probe, "run_probe", run_probe)
        return self

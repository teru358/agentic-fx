"""Indicator fixture runs through the production sandbox (contract run and analysis probe)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import indicator_probe as probe
from agentic_fx.plugin.canonical_fixture import aggregate_to, build_canonical_5m
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.sandbox import PluginSession, SandboxError, inherited_filter_preflight

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"

_reason = inherited_filter_preflight()
needs_sandbox = pytest.mark.skipif(_reason is not None,
                                   reason=f"requires a runnable PluginSession sandbox ({_reason})")


@pytest.fixture(scope="module")
def settings():
    return load_settings(EXAMPLE).plugin


@pytest.fixture(scope="module")
def hourly():
    return aggregate_to(build_canonical_5m(days=200), "1h")


@pytest.fixture(scope="module")
def bars_4096(hourly):
    return hourly.tail(probe.PROBE_BARS)


def make_meta(base: Path, name: str, source: str, *, outputs: tuple[str, ...] | None,
              max_bars: int = 200) -> PluginMeta:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(source)
    (d / "config.yaml").write_text("kind: indicator\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return PluginMeta(name=name, kind="indicator", path=d, params={}, timeframe="1h",
                      pairs=(), max_bars=max_bars, content_hash=content_hash(d), outputs=outputs)


OK_PY = """
import numpy as np
import pandas as pd

def compute(df, params):
    ma = df["close"].rolling(5).mean()
    rng = (df["high"] - df["low"]) / df["close"]
    vol = df["volume"].rolling(5).mean()
    return {"ma": ma, "rng": rng, "vol": vol, "last": float(df["close"].iloc[-1])}
"""
OK_OUTPUTS = ("ma", "rng", "vol", "last")

MISMATCH_PY = """
def compute(df, params):
    return {"b": df["close"]}
"""

LATE_PY = """
import pandas as pd

def compute(df, params):
    return {"v": df["close"].rolling(300).mean()}
"""

LOG_PY = """
import numpy as np

def compute(df, params):
    return {"v": np.log(df["close"] - 100.0)}
"""

WIDE_PY = """
import pandas as pd

def compute(df, params):
    return {f"c{i}": df["close"] * (1.0 + i * 0.0013) + df["high"] * 0.00017 for i in range(32)}
"""
WIDE_OUTPUTS = tuple(f"c{i}" for i in range(32))

SLOW_PY = """
def compute(df, params):
    if len(df) > 1000:
        x = 0
        for i in range(10 ** 10):
            x += i
    return {"v": df["close"]}
"""

RAISE_LONG_PY = """
def compute(df, params):
    if len(df) > 1000:
        raise ValueError("boom on the long run")
    return {"v": df["close"]}
"""

RAISE_PY = """
def compute(df, params):
    raise ValueError("boom")
"""

LOAD_RAISE_PY = """
raise RuntimeError("fails at import")

def compute(df, params):
    return {"v": df["close"]}
"""

SPIN_PY = """
def compute(df, params):
    x = 0
    while True:
        x += 1
"""


def contract_of(tmp_path, settings, hourly, name, source, outputs, **kw):
    meta = make_meta(tmp_path, name, source, outputs=outputs, **kw)
    with probe.open_session(meta, settings) as session:
        return meta, probe.run_contract(session, meta, hourly)


# --- contract run -------------------------------------------------------------------

@needs_sandbox
def test_contract_run_returns_series_and_scalars_over_the_max_bars_tail(tmp_path, settings, hourly):
    meta, res = contract_of(tmp_path, settings, hourly, "ok", OK_PY, OK_OUTPUTS)
    assert res.failure is None and not res.all_nan
    assert set(res.outputs) == set(OK_OUTPUTS)
    assert isinstance(res.outputs["ma"], pd.Series) and len(res.outputs["ma"]) == meta.max_bars
    assert res.outputs["ma"].index.equals(hourly.tail(meta.max_bars).index)
    assert isinstance(res.outputs["last"], float)
    assert res.outputs["last"] == pytest.approx(float(hourly["close"].iloc[-1]))
    assert np.isnan(res.outputs["ma"].iloc[0]) and np.isfinite(res.outputs["ma"].iloc[-1])


@needs_sandbox
def test_outputs_mismatch_is_a_fixture_failure(tmp_path, settings, hourly):
    _, res = contract_of(tmp_path, settings, hourly, "mm", MISMATCH_PY, ("a",))
    assert res.outputs is None and res.failure == "outputs_mismatch"


@needs_sandbox
def test_exception_in_compute_is_exception(tmp_path, settings, hourly):
    _, res = contract_of(tmp_path, settings, hourly, "rz", RAISE_PY, ("v",))
    assert res.failure == "exception"


@needs_sandbox
def test_load_time_failure_is_a_candidate_failure_not_environment(tmp_path, settings):
    meta = make_meta(tmp_path, "lr", LOAD_RAISE_PY, outputs=("v",))
    with pytest.raises(probe.FixtureFailure) as err:
        with probe.open_session(meta, settings):
            pass
    assert err.value.detail == "exception"


@needs_sandbox
def test_timeout_and_cpu_limit_use_the_production_limits(tmp_path, settings, hourly):
    quick = settings.model_copy(update={"sandbox_timeout_sec": 1.0, "sandbox_session_cpu_sec": 30})
    _, res = contract_of(tmp_path, quick, hourly, "spin1", SPIN_PY, ("v",))
    assert res.failure == "timeout"
    cpu = settings.model_copy(update={"sandbox_timeout_sec": 20.0, "sandbox_session_cpu_sec": 1})
    _, res = contract_of(tmp_path, cpu, hourly, "spin2", SPIN_PY, ("v",))
    assert res.failure == "cpu_limit"


@needs_sandbox
def test_all_nan_at_max_bars_is_flagged_but_finite_on_the_long_probe(tmp_path, settings, hourly, bars_4096):
    meta = make_meta(tmp_path, "late", LATE_PY, outputs=("v",))
    with probe.open_session(meta, settings) as session:
        res = probe.run_contract(session, meta, hourly)
        long = probe.run_probe(session, meta, bars_4096)
    assert res.outputs is not None and res.all_nan
    assert long.ok and long.all_nan_runs == ()


# --- analysis probe ----------------------------------------------------------------

@needs_sandbox
def test_probe_runs_the_four_scalings_and_classifies_columns(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "ok", OK_PY, outputs=OK_OUTPUTS)
    with probe.open_session(meta, settings) as session:
        res = probe.run_probe(session, meta, bars_4096)
    assert res.ok and res.all_nan_runs == ()
    assert list(res.runs) == ["k1", "k100", "k001", "vol100"]
    entry = probe.fixture_cache_entry(res)
    assert entry.classifications["ma"] == ("proportional", "invariant")
    assert entry.classifications["rng"] == ("invariant", "invariant")
    assert entry.classifications["vol"] == ("invariant", "neither")
    assert "last" not in entry.classifications
    k100, k1 = res.runs["k100"]["ma"], res.runs["k1"]["ma"]
    assert np.allclose(k100.dropna(), k1.dropna() * 100)


@needs_sandbox
def test_all_nan_at_k001_is_reported(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "logk", LOG_PY, outputs=("v",))
    with probe.open_session(meta, settings) as session:
        res = probe.run_probe(session, meta, bars_4096)
    assert res.ok and res.all_nan_runs == ("k001",)


@needs_sandbox
def test_thirty_two_outputs_pass_the_probe_but_one_call_hits_the_output_limit(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "wide", WIDE_PY, outputs=WIDE_OUTPUTS)
    with probe.open_session(meta, settings) as session:
        res = probe.run_probe(session, meta, bars_4096)
    assert res.ok and all(len(outs) == 32 for outs in res.runs.values())
    with probe.open_session(meta, settings) as session:
        with pytest.raises(SandboxError):
            session.call({"df": bars_4096, "params": {}})


@needs_sandbox
def test_a_candidate_slow_only_on_long_input_is_probe_timeout_within_the_wall_clock(
        tmp_path, settings, hourly, bars_4096):
    import time
    meta = make_meta(tmp_path, "slow", SLOW_PY, outputs=("v",))
    with probe.open_session(meta, settings) as session:
        assert probe.run_contract(session, meta, hourly).failure is None
    with probe.open_session(meta, settings) as session:
        started = time.monotonic()
        res = probe.run_probe(session, meta, bars_4096, wall_clock_max_sec=1.5)
        elapsed = time.monotonic() - started
    assert res.failure == "probe_timeout"
    assert elapsed < 5.0


@needs_sandbox
def test_exception_on_the_long_input_is_probe_exception(tmp_path, settings, hourly, bars_4096):
    meta = make_meta(tmp_path, "rl", RAISE_LONG_PY, outputs=("v",))
    with probe.open_session(meta, settings) as session:
        assert probe.run_contract(session, meta, hourly).failure is None
    with probe.open_session(meta, settings) as session:
        res = probe.run_probe(session, meta, bars_4096)
    assert res.failure == "probe_exception"


# --- pure behaviour (no sandbox) -------------------------------------------------------

class _FakeSession:
    def __init__(self, outputs):
        self.calls = []
        self.outputs = outputs

    def call(self, payload, *, timeout_sec=None):
        self.calls.append((payload, timeout_sec))
        keys = payload.get("select") or list(self.outputs)
        return {k: [1.0] * len(payload["df"]) for k in keys}


def _fake_meta(outputs):
    return PluginMeta(name="f", kind="indicator", path=Path("."), params={}, timeframe="1h",
                      pairs=(), max_bars=200, content_hash="0" * 64, outputs=outputs)


def test_output_groups_hold_at_most_eight_columns_and_cover_every_output(bars_4096):
    outputs = tuple(f"c{i}" for i in range(19))
    fake = _FakeSession(outputs)
    res = probe.run_probe(fake, _fake_meta(outputs), bars_4096)
    assert res.ok and len(fake.calls) == 4 * 3
    sizes = [len(p["select"]) for p, _ in fake.calls]
    assert max(sizes) == 8 and sorted(set(sizes)) == [3, 8]
    first_run = [p["select"] for p, _ in fake.calls[:3]]
    assert [c for g in first_run for c in g] == list(outputs)


def test_probe_scales_only_price_columns_and_volume_in_the_last_run(bars_4096):
    fake = _FakeSession(("v",))
    probe.run_probe(fake, _fake_meta(("v",)), bars_4096)
    dfs = [p["df"] for p, _ in fake.calls]
    base = bars_4096
    assert dfs[0].equals(base)
    assert np.allclose(dfs[1][["open", "high", "low", "close"]], base[["open", "high", "low", "close"]] * 100)
    assert np.allclose(dfs[2]["close"], base["close"] * 0.01)
    assert dfs[1]["volume"].equals(base["volume"]) and dfs[2]["volume"].equals(base["volume"])
    assert dfs[3][["open", "high", "low", "close"]].equals(base[["open", "high", "low", "close"]])
    assert np.allclose(dfs[3]["volume"], base["volume"] * 100)


def test_exhausted_wall_clock_stops_before_the_next_call(bars_4096):
    ticks = iter([0.0, 0.5, 100.0, 100.0, 100.0])
    fake = _FakeSession(("v",))
    res = probe.run_probe(fake, _fake_meta(("v",)), bars_4096, wall_clock_max_sec=60.0,
                          clock=lambda: next(ticks))
    assert res.failure == "probe_timeout" and len(fake.calls) == 1
    assert fake.calls[0][1] == pytest.approx(59.5)


def test_each_call_gets_the_remaining_wall_clock_as_its_limit(bars_4096):
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    fake = _FakeSession(("v",))
    probe.run_probe(fake, _fake_meta(("v",)), bars_4096, wall_clock_max_sec=10.0,
                    clock=lambda: next(ticks))
    assert [t for _, t in fake.calls] == [9.0, 8.0, 7.0, 6.0]


@pytest.mark.parametrize("code,message,probe_mode,expected", [
    ("cpu_limit", "x", False, "cpu_limit"),
    ("timeout", "x", False, "timeout"),
    ("plugin_error", "indicator result keys ['b'] do not match declared outputs ['a']", False, "outputs_mismatch"),
    ("plugin_error", "ValueError: boom", False, "exception"),
    ("crashed", "x", False, "exception"),
    ("protocol_error", "x", False, "exception"),
    ("timeout", "x", True, "probe_timeout"),
    ("cpu_limit", "x", True, "probe_timeout"),
    ("plugin_error", "ValueError: boom", True, "probe_exception"),
    ("crashed", "x", True, "probe_exception"),
])
def test_candidate_side_failures_map_to_fixture_details(code, message, probe_mode, expected):
    assert probe.classify_sandbox_error(SandboxError(message, code=code), probe=probe_mode) == expected


@pytest.mark.parametrize("probe_mode", [False, True])
def test_sandbox_unavailable_is_an_environment_error_not_a_candidate_failure(probe_mode):
    with pytest.raises(probe.IndicatorEnvironmentError):
        probe.classify_sandbox_error(SandboxError("x", code="sandbox_unavailable"), probe=probe_mode)


def test_environment_failure_during_session_start_is_raised_as_environment(monkeypatch, tmp_path, settings):
    def refuse(self):
        raise SandboxError("unavailable", code="sandbox_unavailable")

    monkeypatch.setattr(PluginSession, "__enter__", refuse)
    meta = make_meta(tmp_path, "env", OK_PY, outputs=OK_OUTPUTS)
    with pytest.raises(probe.IndicatorEnvironmentError):
        with probe.open_session(meta, settings):
            pass


def test_environment_failure_inside_a_call_is_raised_by_run_contract(hourly):
    class Env:
        def call(self, payload, *, timeout_sec=None):
            raise SandboxError("unavailable", code="sandbox_unavailable")

    with pytest.raises(probe.IndicatorEnvironmentError):
        probe.run_contract(Env(), _fake_meta(("v",)), hourly)
    with pytest.raises(probe.IndicatorEnvironmentError):
        probe.run_probe(Env(), _fake_meta(("v",)), hourly)


def test_all_nan_detection_counts_scalars_and_series():
    idx = pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC")
    nan = pd.Series([np.nan] * 3, index=idx)
    assert probe.outputs_all_nan({"a": nan, "s": float("nan")})
    assert not probe.outputs_all_nan({"a": nan, "s": 1.0})
    assert not probe.outputs_all_nan({"a": pd.Series([np.nan, 1.0, np.nan], index=idx)})
    assert probe.outputs_all_nan({"a": pd.Series([np.inf, np.nan, np.nan], index=idx)})


# --- select protocol ---------------------------------------------------------------

@needs_sandbox
def test_select_returns_only_the_chosen_outputs_and_rejects_foreign_names(tmp_path, settings, hourly):
    meta = make_meta(tmp_path, "sel", OK_PY, outputs=OK_OUTPUTS)
    with PluginSession(meta, settings=settings) as session:
        out = session.call({"df": hourly.tail(50), "params": {}, "select": ["ma", "last"]})
        assert set(out) == {"ma", "last"}
        with pytest.raises(SandboxError, match="select"):
            session.call({"df": hourly.tail(50), "params": {}, "select": ["nope"]})
        with pytest.raises(SandboxError, match="select"):
            session.call({"df": hourly.tail(50), "params": {}, "select": []})
        with pytest.raises(SandboxError, match="timeout_sec"):
            session.call({"df": hourly.tail(50), "params": {}}, timeout_sec=0)
        assert set(session.call({"df": hourly.tail(50), "params": {}})) == set(OK_OUTPUTS)


# --- cache with a real sandbox build ----------------------------------------------------

@needs_sandbox
def test_second_get_or_build_starts_no_sandbox_and_a_new_fixture_hash_rebuilds(
        tmp_path, settings, bars_4096, monkeypatch):
    from agentic_fx.plugin import indicator_fixture_cache as cache
    from agentic_fx.plugin import sandbox

    meta = make_meta(tmp_path, "cached", OK_PY, outputs=OK_OUTPUTS)
    data_dir = tmp_path / "data"

    def builder():
        with probe.open_session(meta, settings) as session:
            res = probe.run_probe(session, meta, bars_4096)
        assert res.ok
        return probe.fixture_cache_entry(res)

    key = cache.CacheKey(meta.content_hash, "f" * 64, "1h")
    first, built = cache.get_or_build(data_dir, key, builder)
    assert built and first.classifications["ma"] == ("proportional", "invariant")

    starts = []
    real_init = sandbox.PluginSession.__init__

    def counting_init(self, *args, **kwargs):
        starts.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(sandbox.PluginSession, "__init__", counting_init)
    second, built = cache.get_or_build(data_dir, key, builder)
    assert not built and starts == []
    pd.testing.assert_series_equal(second.outputs["ma"], first.outputs["ma"], check_freq=False)
    other = cache.CacheKey(meta.content_hash, "e" * 64, "1h")
    _, built = cache.get_or_build(data_dir, other, builder)
    assert built and starts == [1]

"""Indicator fixture runs through the production sandbox (contract run and analysis probe)."""
from __future__ import annotations

from pathlib import Path

import inspect
import json
import secrets
import time

import numpy as np
import pandas as pd
import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import indicator_probe as probe
from agentic_fx.plugin.canonical_fixture import aggregate_to, build_canonical_5m
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.sandbox import (
    OUTPUTS_MISMATCH_DETAIL, PluginSession, SandboxError, host_preflight,
    inherited_filter_preflight,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"

_reason = host_preflight()[0] or inherited_filter_preflight()
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
    assert entry.classifications["last"] == ("proportional", "invariant")
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


def _fake_meta(outputs, params=None):
    return PluginMeta(name="f", kind="indicator", path=Path("."), params=params or {}, timeframe="1h",
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


@pytest.mark.parametrize("code,message,detail,probe_mode,expected", [
    ("cpu_limit", "x", None, False, "cpu_limit"),
    ("timeout", "x", None, False, "timeout"),
    ("plugin_error", "any wording at all", OUTPUTS_MISMATCH_DETAIL, False, "outputs_mismatch"),
    ("backtest_failed", "any wording at all", OUTPUTS_MISMATCH_DETAIL, False, "outputs_mismatch"),
    ("plugin_error", "indicator result keys ['b'] do not match declared outputs ['a']", None, False,
     "exception"),
    ("plugin_error", "ValueError: boom", None, False, "exception"),
    ("crashed", "x", None, False, "exception"),
    ("protocol_error", "x", None, False, "exception"),
    ("sandbox_unavailable", "x", None, False, "exception"),
    ("timeout", "x", None, True, "probe_timeout"),
    ("cpu_limit", "x", None, True, "probe_timeout"),
    ("plugin_error", "ValueError: boom", None, True, "probe_exception"),
    ("plugin_error", "x", OUTPUTS_MISMATCH_DETAIL, True, "probe_exception"),
    ("crashed", "x", None, True, "probe_exception"),
    ("sandbox_unavailable", "x", None, True, "probe_exception"),
])
def test_candidate_side_failures_map_to_fixture_details(code, message, detail, probe_mode, expected):
    exc = SandboxError(message, code=code, detail=detail)
    assert probe.classify_sandbox_error(exc, probe=probe_mode) == expected


def test_environment_failure_during_session_start_is_raised_as_environment(monkeypatch, tmp_path, settings):
    def refuse(self):
        raise SandboxError("unavailable", code="sandbox_unavailable")

    monkeypatch.setattr(PluginSession, "__enter__", refuse)
    meta = make_meta(tmp_path, "env", OK_PY, outputs=OK_OUTPUTS)
    with pytest.raises(probe.IndicatorEnvironmentError):
        with probe.open_session(meta, settings):
            pass


def test_environment_failure_after_the_load_is_the_candidates(hourly):
    class Env:
        def call(self, payload, *, timeout_sec=None):
            raise SandboxError("unavailable", code="sandbox_unavailable")

    assert probe.run_contract(Env(), _fake_meta(("v",)), hourly).failure == "exception"
    assert probe.run_probe(Env(), _fake_meta(("v",)), hourly).failure == "probe_exception"


def test_a_failed_session_start_is_closed_and_a_clean_one_is_closed_too(monkeypatch, tmp_path, settings):
    closed = []
    monkeypatch.setattr(PluginSession, "close", lambda self: closed.append(1))

    def refuse(self):
        raise SandboxError("unavailable", code="sandbox_unavailable")

    meta = make_meta(tmp_path, "cl", OK_PY, outputs=OK_OUTPUTS)
    with monkeypatch.context() as patch:
        patch.setattr(PluginSession, "__enter__", refuse)
        with pytest.raises(probe.IndicatorEnvironmentError):
            with probe.open_session(meta, settings):
                pass
    assert closed == [1]
    with monkeypatch.context() as patch:
        patch.setattr(PluginSession, "__enter__", lambda self: self)
        with probe.open_session(meta, settings):
            assert closed == [1]
    assert closed == [1, 1]


def test_a_candidate_failure_at_session_start_is_closed_and_classified(monkeypatch, tmp_path, settings):
    closed = []
    monkeypatch.setattr(PluginSession, "close", lambda self: closed.append(1))

    def fail(self):
        raise SandboxError("load failed", code="plugin_error")

    meta = make_meta(tmp_path, "cl2", OK_PY, outputs=OK_OUTPUTS)
    monkeypatch.setattr(PluginSession, "__enter__", fail)
    with pytest.raises(probe.FixtureFailure) as err:
        with probe.open_session(meta, settings):
            pass
    assert err.value.detail == "exception" and closed == [1]


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


# --- one compute per scaling --------------------------------------------------------

COUNTING_PY = """
import pandas as pd

CALLS = []

def compute(df, params):
    CALLS.append(1)
    out = {f"c{i}": df["close"] * (1.0 + i * 0.0013) + df["high"] * 0.00017 for i in range(31)}
    out["calls"] = float(len(CALLS))
    return out
"""
COUNTING_OUTPUTS = tuple(f"c{i}" for i in range(31)) + ("calls",)


@needs_sandbox
def test_thirty_two_outputs_compute_once_per_scaling_not_once_per_group(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "counting", COUNTING_PY, outputs=COUNTING_OUTPUTS)
    with probe.open_session(meta, settings) as session:
        res = probe.run_probe(session, meta, bars_4096)
    assert res.ok and all(len(outs) == 32 for outs in res.runs.values())
    assert [res.runs[label]["calls"] for label in ("k1", "k100", "k001", "vol100")] == [1.0, 2.0, 3.0, 4.0]


@needs_sandbox
def test_every_group_of_a_scaling_is_served_from_the_same_computation(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "samecomp", COUNTING_PY, outputs=COUNTING_OUTPUTS)
    with probe.open_session(meta, settings) as session:
        first = session.call({"df": bars_4096, "params": {}, "select": ["c0", "calls"], "retain": True})
        again = session.call({"df": bars_4096, "params": {}, "select": ["c1", "calls"], "reuse": True})
        fresh = session.call({"df": bars_4096, "params": {}, "select": ["calls"]})
    assert first["calls"] == 1.0 and again["calls"] == 1.0 and fresh["calls"] == 2.0
    assert set(again) == {"c1", "calls"}


@needs_sandbox
def test_reuse_without_a_retained_result_or_with_retain_is_refused_and_a_plain_call_drops_it(
        tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "reuse", COUNTING_PY, outputs=COUNTING_OUTPUTS)
    df = bars_4096
    with probe.open_session(meta, settings) as session:
        with pytest.raises(SandboxError, match="no retained"):
            session.call({"df": df, "params": {}, "select": ["calls"], "reuse": True})
    with probe.open_session(meta, settings) as session:
        session.call({"df": df, "params": {}, "select": ["calls"], "retain": True})
        session.call({"df": df, "params": {}, "select": ["calls"]})
        with pytest.raises(SandboxError, match="no retained"):
            session.call({"df": df, "params": {}, "select": ["calls"], "reuse": True})


@needs_sandbox
@pytest.mark.parametrize("payload,message", [
    ({"retain": True, "reuse": True}, "retain and reuse cannot be combined"),
    ({"retain": "yes"}, "retain must be a bool"),
    ({"reuse": 1}, "reuse must be a bool"),
])
def test_retain_and_reuse_must_be_distinct_booleans(tmp_path, settings, bars_4096, payload, message):
    meta = make_meta(tmp_path, "flags", OK_PY, outputs=OK_OUTPUTS)
    with probe.open_session(meta, settings) as session:
        session.call({"df": bars_4096.tail(50), "params": {}, "retain": True})
        with pytest.raises(SandboxError, match=message):
            session.call({"df": bars_4096.tail(50), "params": {}, **payload})
        assert set(session.call({"df": bars_4096.tail(50), "params": {}, "reuse": True})) == set(OK_OUTPUTS)


@needs_sandbox
def test_the_worker_refuses_retain_together_with_reuse(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "wflags", OK_PY, outputs=OK_OUTPUTS)
    with PluginSession(meta, settings=settings) as session:
        assert _raw_call(session, bars_4096.tail(50), retain=True)["ok"] is True
        both = _raw_call(session, bars_4096.tail(50), retain=True, reuse=True)
        assert both["ok"] is False and "no retained" in both["error"]


@needs_sandbox
def test_a_mismatch_found_by_the_parents_own_check_carries_the_fixed_detail(
        tmp_path, settings, hourly, monkeypatch):
    meta = make_meta(tmp_path, "pmm", OK_PY, outputs=OK_OUTPUTS)
    real = PluginSession._read_response

    def lying(self, timeout_sec, max_bytes):
        resp = real(self, timeout_sec, max_bytes)
        if resp.get("ok") and isinstance(resp.get("result"), dict):
            resp["result"] = {"zzz": 1.0}
        return resp

    with PluginSession(meta, settings=settings) as session:
        monkeypatch.setattr(PluginSession, "_read_response", lying)
        with pytest.raises(SandboxError) as err:
            session.call({"df": hourly.tail(50), "params": {}})
    assert err.value.detail == OUTPUTS_MISMATCH_DETAIL


@needs_sandbox
def test_outputs_mismatch_carries_a_structured_code_not_a_wording(tmp_path, settings, hourly):
    meta = make_meta(tmp_path, "mmcode", MISMATCH_PY, outputs=("a",))
    with probe.open_session(meta, settings) as session:
        with pytest.raises(SandboxError) as err:
            session.call({"df": hourly.tail(50), "params": {}})
    assert err.value.detail == OUTPUTS_MISMATCH_DETAIL and err.value.code == "plugin_error"
    other = make_meta(tmp_path, "rzcode", RAISE_PY, outputs=("v",))
    with probe.open_session(other, settings) as session:
        with pytest.raises(SandboxError) as err:
            session.call({"df": hourly.tail(50), "params": {}})
    assert err.value.detail is None


def test_a_plain_sandbox_error_has_no_detail():
    assert SandboxError("x").detail is None


def test_probe_asks_the_first_group_to_retain_and_the_rest_to_reuse(bars_4096):
    outputs = tuple(f"c{i}" for i in range(19))
    fake = _FakeSession(outputs)
    probe.run_probe(fake, _fake_meta(outputs), bars_4096)
    flags = [(p.get("retain"), p.get("reuse"), p.get("release")) for p, _ in fake.calls]
    assert flags == [(True, None, None), (None, True, None), (None, True, True)] * 4


def test_a_single_group_or_undeclared_outputs_need_no_retention(bars_4096):
    for outputs in (("v",), tuple(f"c{i}" for i in range(8)), None):
        fake = _FakeSession(("v",))
        probe.run_probe(fake, _fake_meta(outputs), bars_4096)
        assert all("retain" not in p and "reuse" not in p and "release" not in p for p, _ in fake.calls)


# --- pinned details -----------------------------------------------------------------

def test_params_reach_the_contract_run_and_every_probe_call(bars_4096):
    fake = _FakeSession(("v",))
    meta = _fake_meta(("v",), params={"n": 7})
    probe.run_contract(fake, meta, bars_4096)
    probe.run_probe(fake, meta, bars_4096)
    assert len(fake.calls) == 5 and all(p["params"] == {"n": 7} for p, _ in fake.calls)


def test_the_default_wall_clock_is_sixty_seconds(bars_4096):
    assert probe.DEFAULT_WALL_CLOCK_MAX_SEC == 60.0
    assert inspect.signature(probe.run_probe).parameters["wall_clock_max_sec"].default == 60.0
    fake = _FakeSession(("v",))
    probe.run_probe(fake, _fake_meta(("v",)), bars_4096, clock=lambda: 0.0)
    assert fake.calls[0][1] == 60.0


def test_a_call_with_exactly_no_time_left_is_not_made(bars_4096):
    ticks = iter([0.0, 10.0, 10.0, 10.0])
    fake = _FakeSession(("v",))
    res = probe.run_probe(fake, _fake_meta(("v",)), bars_4096, wall_clock_max_sec=10.0,
                          clock=lambda: next(ticks))
    assert res.failure == "probe_timeout" and fake.calls == []


def test_all_nan_counts_infinite_and_empty_outputs_as_having_no_value():
    idx = pd.date_range("2026-01-01", periods=2, freq="1h", tz="UTC")
    assert probe.outputs_all_nan({})
    assert probe.outputs_all_nan({"s": float("inf")})
    assert probe.outputs_all_nan({"s": float("-inf")})
    assert probe.outputs_all_nan({"s": float("-inf"), "t": pd.Series([np.inf, -np.inf], index=idx)})
    assert not probe.outputs_all_nan({"s": float("-inf"), "t": 0.0})
    assert not probe.outputs_all_nan({"s": 0.0})


def test_a_null_scalar_becomes_nan_not_zero():
    idx = pd.date_range("2026-01-01", periods=2, freq="1h", tz="UTC")
    out = probe._to_outputs({"s": None, "l": [1.0, None], "f": 2}, idx)
    assert np.isnan(out["s"]) and out["f"] == 2.0 and isinstance(out["f"], float)
    assert isinstance(out["l"], pd.Series) and np.isnan(out["l"].iloc[1])


# --- the session's select / timeout checks, hit directly -----------------------------------

SIGNAL_PY = "def detect(df, params):\n    return []\n"
SPIN_ALWAYS_PY = """
def compute(df, params):
    x = 0
    while True:
        x += 1
"""


ONE_OUTPUT_PY = """
def compute(df, params):
    return {"v": df["close"]}
"""


@needs_sandbox
@pytest.mark.parametrize("select", ["v", {"v": 1}])
def test_a_select_that_is_not_a_list_is_refused_even_when_its_items_name_an_output(
        tmp_path, settings, hourly, select):
    meta = make_meta(tmp_path, "sel3", ONE_OUTPUT_PY, outputs=("v",))
    with PluginSession(meta, settings=settings) as session:
        with pytest.raises(SandboxError, match="^select must be a non-empty subset"):
            session.call({"df": hourly.tail(50), "params": {}, "select": select})
        resp = _raw_call(session, hourly.tail(50), select=select)
        assert resp["ok"] is False and "select must name outputs" in resp["error"]


@needs_sandbox
@pytest.mark.parametrize("select", [["nope"], [], [["ma"]], [3], "ma", ["ma", "nope"]])
def test_the_session_refuses_a_bad_select_before_the_worker_sees_it(tmp_path, settings, hourly, select):
    meta = make_meta(tmp_path, "sel2", OK_PY, outputs=OK_OUTPUTS)
    with PluginSession(meta, settings=settings) as session:
        with pytest.raises(SandboxError, match="^select must be a non-empty subset of the declared outputs$"):
            session.call({"df": hourly.tail(50), "params": {}, "select": select})
        assert set(session.call({"df": hourly.tail(50), "params": {}, "select": ("ma",)})) == {"ma"}


@needs_sandbox
def test_select_is_only_valid_for_an_indicator_call(tmp_path, settings, hourly):
    d = tmp_path / "sig"
    d.mkdir()
    (d / "plugin.py").write_text(SIGNAL_PY)
    (d / "config.yaml").write_text("kind: signal\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    meta = PluginMeta(name="sig", kind="signal", path=d, params={}, timeframe="1h", pairs=(),
                      max_bars=200, content_hash=content_hash(d), outputs=("v",))
    with PluginSession(meta, settings=settings) as session:
        with pytest.raises(SandboxError, match="^select must be a non-empty subset"):
            session.call({"df": hourly.tail(50), "params": {}, "select": ["v"]})


def _raw_call(session, df, **fields):
    from agentic_fx.plugin import sandbox as sb

    request = {"df": sb._df_to_wire(df), "params": {}, "id": secrets.token_hex(8), **fields}
    session._write_line(json.dumps(request) + "\n", deadline=time.monotonic() + 20.0)
    return session._read_response(20.0, session._settings.sandbox_output_max_bytes)


@needs_sandbox
@pytest.mark.parametrize("select", [[], ["nope"], ["ma", "nope"], [3], "ma", {"a": 1}])
def test_the_worker_itself_refuses_a_bad_select(tmp_path, settings, hourly, select):
    meta = make_meta(tmp_path, "wsel", OK_PY, outputs=OK_OUTPUTS)
    with PluginSession(meta, settings=settings) as session:
        resp = _raw_call(session, hourly.tail(50), select=select)
        assert resp["ok"] is False and "select must name outputs" in resp["error"]
        good = _raw_call(session, hourly.tail(50), select=["ma", "last"])
        assert good["ok"] is True and set(good["result"]) == {"ma", "last"}


@needs_sandbox
def test_the_worker_reports_an_outputs_mismatch_with_its_fixed_code(tmp_path, settings, hourly):
    meta = make_meta(tmp_path, "wmm", MISMATCH_PY, outputs=("a",))
    with PluginSession(meta, settings=settings) as session:
        resp = _raw_call(session, hourly.tail(50))
        assert resp["ok"] is False and resp["error_code"] == "outputs_mismatch"
    other = make_meta(tmp_path, "wrz", RAISE_PY, outputs=("v",))
    with PluginSession(other, settings=settings) as session:
        resp = _raw_call(session, hourly.tail(50))
        assert resp["ok"] is False and "error_code" not in resp


@needs_sandbox
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "1", True, 0, -1.0])
def test_a_bad_call_timeout_is_refused(tmp_path, settings, hourly, bad):
    meta = make_meta(tmp_path, "tmo", OK_PY, outputs=OK_OUTPUTS)
    with PluginSession(meta, settings=settings) as session:
        with pytest.raises(SandboxError, match="timeout_sec"):
            session.call({"df": hourly.tail(50), "params": {}}, timeout_sec=bad)
        assert set(session.call({"df": hourly.tail(50), "params": {}})) == set(OK_OUTPUTS)


@needs_sandbox
def test_a_call_timeout_cannot_exceed_the_session_limit(tmp_path, settings, hourly):
    quick = settings.model_copy(update={"sandbox_timeout_sec": 1.0, "sandbox_session_cpu_sec": 30})
    meta = make_meta(tmp_path, "tmo2", SPIN_ALWAYS_PY, outputs=("v",))
    with PluginSession(meta, settings=quick) as session:
        started = time.monotonic()
        with pytest.raises(SandboxError) as err:
            session.call({"df": hourly.tail(50), "params": {}}, timeout_sec=25)
        assert err.value.code == "timeout" and time.monotonic() - started < 10.0
    with PluginSession(make_meta(tmp_path, "tmo3", SPIN_ALWAYS_PY, outputs=("v",)),
                       settings=settings.model_copy(update={"sandbox_timeout_sec": 25.0,
                                                           "sandbox_session_cpu_sec": 30})) as session:
        started = time.monotonic()
        with pytest.raises(SandboxError) as err:
            session.call({"df": hourly.tail(50), "params": {}}, timeout_sec=1.0)
        assert err.value.code == "timeout" and time.monotonic() - started < 10.0


# --- probe consistency, bounds, retention, scalar classes -----------------------------

class _ShiftingSession(_FakeSession):
    """Answers like _FakeSession, except that the run numbered ``bad_run`` differs."""

    def __init__(self, outputs, bad_run, mode):
        super().__init__(outputs)
        self.bad_run, self.mode = bad_run, mode

    def call(self, payload, *, timeout_sec=None):
        result = super().call(payload, timeout_sec=timeout_sec)
        if len(self.calls) - 1 == self.bad_run:
            if self.mode == "extra":
                result["surprise"] = [1.0] * len(payload["df"])
            elif self.mode == "missing":
                result.pop(next(iter(result)))
            elif self.mode == "scalar":
                key = next(iter(result))
                result[key] = 1.0
        return result


@pytest.mark.parametrize("mode", ["extra", "missing", "scalar"])
@pytest.mark.parametrize("bad_run", [1, 3])
def test_a_scaled_run_with_other_keys_or_shape_than_k1_is_a_probe_exception(bars_4096, mode, bad_run):
    fake = _ShiftingSession(("a", "b"), bad_run, mode)
    res = probe.run_probe(fake, _fake_meta(None), bars_4096)
    assert res.failure == "probe_exception" and not res.ok
    assert "k1" in res.runs and list(res.runs)[-1] != ["k100", "k001", "vol100"][bad_run - 1]


def test_a_consistent_probe_is_still_ok(bars_4096):
    res = probe.run_probe(_FakeSession(("a", "b")), _fake_meta(None), bars_4096)
    assert res.ok and len(res.runs) == 4


def test_run_probe_cuts_the_bars_to_the_probe_length_itself(hourly):
    longer = pd.concat([hourly, hourly]).iloc[-(probe.PROBE_BARS + 500):]
    assert len(longer) > probe.PROBE_BARS
    fake = _FakeSession(("v",))
    probe.run_probe(fake, _fake_meta(("v",)), longer)
    assert [len(p["df"]) for p, _ in fake.calls] == [probe.PROBE_BARS] * 4
    assert fake.calls[0][0]["df"].index.equals(longer.index[-probe.PROBE_BARS:])


def _scalar_probe(k1, k100, k001, vol100):
    return probe.ProbeResult(runs={"k1": {"s": k1}, "k100": {"s": k100}, "k001": {"s": k001},
                                   "vol100": {"s": vol100}})


@pytest.mark.parametrize("values,expected", [
    ((2.0, 2.0, 2.0, 2.0), ("invariant", "invariant")),
    ((2.0, 200.0, 0.02, 2.0), ("proportional", "invariant")),
    ((2.0, 3.0, 5.0, 7.0), ("neither", "neither")),
    ((2.0, 200.0, 0.02, 9.0), ("proportional", "neither")),
])
def test_scalar_outputs_are_scale_classified_in_three_values(values, expected):
    entry = probe.fixture_cache_entry(_scalar_probe(*values))
    assert entry.classifications == {"s": expected}


def test_series_and_scalar_classes_are_both_present():
    idx = pd.date_range("2026-01-05", periods=30, freq="1h", tz="UTC")
    base = pd.Series(np.arange(1.0, 31.0), index=idx)
    runs = {"k1": {"v": base, "s": 2.0}, "k100": {"v": base * 100, "s": 200.0},
            "k001": {"v": base * 0.01, "s": 0.02}, "vol100": {"v": base, "s": 2.0}}
    entry = probe.fixture_cache_entry(probe.ProbeResult(runs=runs))
    assert entry.classifications == {"v": ("proportional", "invariant"),
                                     "s": ("proportional", "invariant")}


RETAIN_RAISES_PY = """
CALLS = []

def compute(df, params):
    CALLS.append(1)
    if len(CALLS) == 2:
        raise ValueError("second compute fails")
    return {"a": df["close"], "b": df["close"] * 2.0}
"""


@needs_sandbox
def test_a_failed_retain_leaves_nothing_to_reuse(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "retfail", RETAIN_RAISES_PY, outputs=("a", "b"))
    df = bars_4096.tail(50)
    with probe.open_session(meta, settings) as session:
        session.call({"df": df, "params": {}, "select": ["a"], "retain": True})
        with pytest.raises(SandboxError, match="second compute fails"):
            session.call({"df": df, "params": {}, "select": ["a"], "retain": True})
        with pytest.raises(SandboxError, match="no retained"):
            session.call({"df": df, "params": {}, "select": ["b"], "reuse": True})


@needs_sandbox
def test_release_with_the_last_reuse_drops_the_retained_result(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "release", COUNTING_PY, outputs=COUNTING_OUTPUTS)
    df = bars_4096.tail(50)
    with probe.open_session(meta, settings) as session:
        session.call({"df": df, "params": {}, "select": ["c0"], "retain": True})
        kept = session.call({"df": df, "params": {}, "select": ["c1"], "reuse": True})
        last = session.call({"df": df, "params": {}, "select": ["c2"], "reuse": True, "release": True})
        with pytest.raises(SandboxError, match="no retained"):
            session.call({"df": df, "params": {}, "select": ["c3"], "reuse": True})
    assert set(kept) == {"c1"} and set(last) == {"c2"}


@needs_sandbox
def test_release_without_reuse_is_refused_by_the_parent(tmp_path, settings, bars_4096):
    meta = make_meta(tmp_path, "relbad", OK_PY, outputs=OK_OUTPUTS)
    with probe.open_session(meta, settings) as session:
        with pytest.raises(SandboxError, match="release is only valid together with reuse"):
            session.call({"df": bars_4096.tail(50), "params": {}, "release": True})

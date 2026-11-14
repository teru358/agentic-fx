"""A stored, non-deployed strategy version and canned backtests, for the audit entry.

Everything lives under ``tmp_path``. The real shared gate runs; only the sandboxed pieces
(the gate's pytest, the replay and the intent source) are replaced, and the replay records
the rows a real run would hand to the gate."""
from __future__ import annotations

import contextlib
import signal
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from agentic_fx.plugin import history_audit as audit
from agentic_fx.plugin import loader
from agentic_fx.plugin.gate_pytest import GateResult
from tests.history_support import fill_history
from tests.plugin.reevaluation_world import (
    CONFIG, SOURCE, RealWorld, strategy_py)
from tests.plugin.test_strategy_gate import _metrics

UTC = timezone.utc
TUESDAY = datetime(2026, 11, 10, 12, 0, 20, tzinfo=UTC)
NAME = "sma"


class Blocked(BaseException):
    """A BaseException, so that the code under test cannot swallow it as an I/O error."""


@contextlib.contextmanager
def never_block(seconds: int = 10):
    """Fail fast (instead of stalling the whole run) if the code blocks on a special file."""
    def alarm(signum, frame):
        raise Blocked("blocked on a special file")
    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class Replay:
    """Stands in for ``holdout.run_in_sample`` / ``run_holdout_gate``."""

    def __init__(self, in_sample=None, holdout=None):
        self.metrics = {
            "in_sample": in_sample or (lambda pair: _metrics(trades=40, pf=1.5)),
            "holdout_gate": holdout or (lambda pair: _metrics(trades=40, pf=1.5))}
        self.calls: list[tuple[str, str]] = []

    def runner(self, scope: str):
        def run(settings, *, history_conn, symbol, dataset, intent_source, eval_timeframe,
                plugin_ref, content_hash, kind, now, record_fn=None):
            self.calls.append((scope, symbol))
            metrics = self.metrics[scope](symbol)
            if record_fn is not None:
                end = now.replace(second=0, microsecond=0)
                record_fn({
                    "scope": scope, "plugin_ref": plugin_ref, "content_hash": content_hash,
                    "kind": kind, "pair": symbol, "timeframe": eval_timeframe,
                    "source": dataset.source, "base_interval": dataset.base_interval,
                    "params": {}, "period": (end - timedelta(days=30), end),
                    "metrics": dict(metrics), "settings_hash": "s", "core_commit": "c",
                    "initial_balance": 123456.0, "now": end})
            return dict(metrics)
        return run


class Gate:
    """Stands in for the sandboxed pytest of the shared gate; counts its calls.

    By default it reports what the real worker does: a ready worker and pytest's own exit code
    equal to the process exit code. Pass `ready` / `pytest_exit` / `hash_changed` to model an
    exit that the worker did not describe."""

    _SAME = object()

    def __init__(self, passed: bool = True, returncode: int = 0, *, ready: bool | None = None,
                 pytest_exit=_SAME, hash_changed: bool = False):
        self.passed, self.returncode, self.calls = passed, returncode, 0
        self.ready = (returncode >= 0) if ready is None else ready
        self.pytest_exit = (returncode if returncode >= 0 else None) \
            if pytest_exit is Gate._SAME else pytest_exit
        self.hash_changed = hash_changed

    def __call__(self, plugin_dir, *, settings):
        self.calls += 1
        return GateResult(self.passed, self.returncode, "", 0.01, worker_ready=self.ready,
                          pytest_exit=self.pytest_exit, hash_changed=self.hash_changed)


class AuditWorld(RealWorld):
    def __init__(self, tmp_path, monkeypatch, *, history: str | None = "normal",
                 replay: Replay | None = None, gate: Gate | None = None):
        super().__init__(tmp_path, history=False)
        self.tmp_path = tmp_path
        self.replay = replay or Replay()
        self.gate = gate or Gate()
        monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                            self.replay.runner("in_sample"))
        monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
                            self.replay.runner("holdout_gate"))
        monkeypatch.setattr(
            "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
            lambda meta, **kw: MagicMock(close=lambda: None, cpu_sec=0.0))
        monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.require_reference_history",
                            lambda *a, **kw: None)
        monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", self.gate)
        if history is not None:
            fill_history(self.conn, source=SOURCE, pairs=["USDJPY"], now=TUESDAY,
                         scenario=history)
        self.report_dir = tmp_path / "reports"
        self.work_root = tmp_path / "work"

    def stored(self, tag: str = "a", *, py: str | None = None, config: str = CONFIG):
        """A stored, approved, non-deployed version: the target and its old approval id."""
        version = self.strategy(tag, py or strategy_py(), config=config, name=NAME)
        old_id = self.approve(NAME, version)
        return version, audit.AuditTarget(
            old_id, NAME, version.name, loader.content_hash(version), {})

    def run(self, target, **kwargs):
        kwargs.setdefault("clock", lambda: TUESDAY)
        return audit.audit_non_deployed_version(
            self.conn, target, plugins_root=self.root, settings=self.settings,
            report_dir=self.report_dir, work_root=self.work_root, **kwargs)

    def counts(self) -> dict:
        names = ("backtest_runs", "approval_requests", "plugin_deploy_ops",
                 "plugin_switch_journal", "signals")
        out = {}
        for table in names:
            try:
                out[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:  # noqa: BLE001 - a table this schema does not have
                out[table] = None
        out["total_changes"] = self.conn.total_changes
        return out

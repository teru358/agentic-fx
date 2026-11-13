"""A deployed strategy and a small synthetic history, for running the real gate.

Everything lives under ``tmp_path``: the database, the plugins directory, the history. The
history is a deterministic 24 hour wave on a 5 minute base grid; the strategy is the
three-rising-closes breakout, so its result is a pure function of the history and ``now``.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.config import load_settings
from agentic_fx.plugin import loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import ohlcv
from tests.plugin.deploy_world import World, at

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
PAIR = "USDJPY"
SOURCE = "dukascopy"
BASE = "5m"
BARS_START = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
BARS_COUNT = 63 * 288 + 96
STARTED = datetime(2026, 11, 8, 3, 17, 42, tzinfo=timezone.utc)

CONFIG = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
          "max_bars: 50\nparams: {}\n")
TEST_PY = "def test_plugin():\n    pass\n"


def strategy_py(take_profit: float = 0.6, stop_loss: float = 0.3, note: str = "") -> str:
    marker = f"_NOTE = {note!r}\n\n" if note else ""
    return (
        "import pandas as pd\n\n"
        f"{marker}"
        "def evaluate(df, indicators, signals, params):\n"
        "    hold = {'action': 'hold', 'rationale': 'flat'}\n"
        "    c = df['close'].astype(float)\n"
        "    if len(c) < 3:\n"
        "        return hold\n"
        "    if c.iloc[-1] > c.iloc[-2] > c.iloc[-3]:\n"
        "        close = float(c.iloc[-1])\n"
        "        return {'action': 'open', 'direction': 'long',\n"
        "                'entry_type': 'market',\n"
        f"                'stop_loss': close - {stop_loss!r},\n"
        f"                'take_profit': close + {take_profit!r},\n"
        "                'rationale': 'up'}\n"
        "    return hold\n")


HOLD_PY = ("def evaluate(df, indicators, signals, params):\n"
           "    return {'action': 'hold', 'rationale': 'flat'}\n")


def synthetic_rows() -> list[tuple]:
    rows = []
    previous = 150.0
    for k in range(BARS_COUNT):
        close = 150.0 + 2.0 * math.sin(2 * math.pi * k / 288) + 0.5 * math.sin(
            2 * math.pi * k / 2016)
        open_ = previous if k else 150.0
        stamp = (BARS_START + timedelta(minutes=5 * k)).isoformat()
        rows.append((PAIR, BASE, stamp, open_, max(open_, close) + 0.05,
                     min(open_, close) - 0.05, close, 100, 0.01))
        previous = close
    return rows


def real_settings(*, eval_source: str = SOURCE):
    settings = load_settings(EXAMPLE)
    settings.backtest.eval_source = eval_source
    settings.backtest.base_interval = BASE
    settings.backtest.holdout_months = 1
    settings.improve.gate.max_trades_per_eval_bar = None
    return settings


class RealWorld(World):
    """A `World` with real strategy versions and history."""

    def __init__(self, tmp_path: Path, *, history: bool = True):
        super().__init__(tmp_path)
        self.settings = real_settings()
        if history:
            ohlcv.import_history_bars(self.conn, synthetic_rows(), source=SOURCE)

    def strategy(self, tag: str, py: str, *, config: str = CONFIG, name: str = "sma") -> Path:
        staging = self.root / ".versions" / name / f"tmp-{tag}"
        staging.mkdir(parents=True)
        (staging / "plugin.py").write_text(py)
        (staging / "config.yaml").write_text(config)
        (staging / "test_plugin.py").write_text(TEST_PY)
        digest = loader.artifact_hash_bytes(*(p.read_bytes() for p in (
            staging / "plugin.py", staging / "config.yaml", staging / "test_plugin.py")))
        final = staging.with_name(digest)
        staging.rename(final)
        return final

    def bless_like(self, version: Path, minute: int, *, payload: dict | None = None,
                   name: str = "sma") -> int:
        """An approved row in the form human `bless` writes (metrics only), deployed."""
        merged = {"name": name, "kind": "strategy",
                  "content_hash": loader.content_hash(version),
                  "artifact_hash": version.name, "candidate_origin": "human",
                  "candidate_path": f"plugins/_human/{name}"}
        merged.update(payload or {})
        approval_id = approvals_store.create(self.conn, "plugin", merged, at(minute))
        approvals_store.apply_decision(self.conn, approval_id, "approved",
                                       decided_by="human", now=at(minute))
        self.journal(name, "approve", None, version, minute, approval_id)
        self.link(name, version)
        return approval_id


class FakeGate:
    """Stands in for the shared gate: records its calls, hands back canned results.

    ``during`` runs inside the evaluation, which is outside every plugin lock.
    """

    def __init__(self, *, evaluable: bool = True, reason: str = "", rows: bool = True,
                 trades: int = 40, floor: str = "", during=None, error: Exception | None = None):
        self.evaluable, self.reason, self.with_rows = evaluable, reason, rows
        self.trades, self.floor = trades, floor
        self.during, self.error = during, error
        self.calls: list[dict] = []

    def metrics(self, scope: str) -> dict:
        return {"trades": self.trades, "pf": 1.5 if scope == "in_sample" else 1.25,
                "win_rate": 0.5, "avg_r": 0.2, "max_drawdown": 0.05, "total_pnl": 10.0,
                "evaluable": True, "fallback_spread_used": False, "eval_bars": 100}

    def row(self, snapshot, scope: str, pair: str, now) -> dict:
        from agentic_fx.backtest.holdout import _normalize_now
        end = _normalize_now(now, "5m")
        return {
            "scope": scope, "plugin_ref": f"plugins/{snapshot.meta.name}",
            "content_hash": snapshot.meta.content_hash, "kind": "strategy", "pair": pair,
            "timeframe": "1h", "source": SOURCE, "base_interval": BASE, "params": {},
            "period": (end - timedelta(days=30), end), "metrics": self.metrics(scope),
            "settings_hash": "s", "core_commit": "c", "initial_balance": 1e6, "now": end}

    def __call__(self, conn, snapshot, *, settings, now, rows):
        from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
        self.calls.append({"name": snapshot.meta.name, "now": now})
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        pairs = list(snapshot.meta.pairs)
        if self.with_rows:
            for pair in pairs:
                rows.append(self.row(snapshot, "in_sample", pair, now))
        if not self.evaluable:
            return StrategyGateVerdict(
                evaluable=False, observation_reason=self.reason,
                unevaluable_kind="insufficient_trades")
        if self.with_rows:
            for pair in pairs:
                rows.append(self.row(snapshot, "holdout_gate", pair, now))
        from tests.history_support import ready_evidence
        return StrategyGateVerdict(
            evaluable=True,
            candidate_metrics={p: self.metrics("in_sample") for p in pairs},
            holdout_metrics={p: self.metrics("holdout") for p in pairs},
            floor_reason=self.floor, floor_detail="detail" if self.floor else "",
            history_guard=ready_evidence(
                source=settings.backtest.eval_source,
                base_interval=settings.backtest.base_interval, pairs=pairs, now=now,
                holdout_months=settings.backtest.holdout_months))

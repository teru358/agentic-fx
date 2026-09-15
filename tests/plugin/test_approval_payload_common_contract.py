"""4 payload 系統 (approval.submit_plugin / switch.submit_candidate /
switch.bless_candidate / improve_loop._build_approval_payload) の共通契約
テスト (I2, codex 段階2/3 是正 1周目)。

裁定 (verified-s3-codex.md I2): 旧 `submit_plugin` は `eval_source` を
kind に依らず実値で載せ (indicator/signal にも「使ってもいない source」を
露出)、残り 3 系統 (`switch.submit_candidate`/`bless_candidate`/
`improve_loop._build_approval_payload`) は `eval_source` キー自体を
持たなかった。4 系統とも同じ規約 (strategy は dataset 値 + 写像後
eval_timeframe、indicator/signal は 3 キーとも null) を満たすことを、
同一 fixture (strategy timeframe="1d" / indicator) で回して比較する。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin import approval, switch
from agentic_fx.plugin.gate_pytest import GateResult
from agentic_fx.plugin.loader import PluginMeta, content_hash as real_content_hash
from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
from agentic_fx.store.db import connect, init_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.yaml.example"

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)

# eval_source/base_interval を既定 (dukascopy/1m) から動かして「既定値へ
# 偶然一致する」変異を検出できるようにする。
SETTINGS = load_settings(_EXAMPLE).model_copy(update={
    "backtest": load_settings(_EXAMPLE).backtest.model_copy(
        update={"eval_source": "mt5", "base_interval": "5m"}),
    "plugin": load_settings(_EXAMPLE).plugin.model_copy(
        update={"producer_source": "twelvedata"})})

INDICATOR_PY = "def compute(df, params):\n    return {'v': 1.0}\n"
STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
              "    return {'action': 'hold', 'rationale': 'x'}\n")
TEST_PY_OK = "def test_x():\n    pass\n"

_CONTRACT_KEYS = {
    "eval_source", "base_interval", "eval_timeframe", "live_source"}


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _write_plugin(base: Path, name: str, *, kind: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    if kind == "indicator":
        (d / "plugin.py").write_text(INDICATOR_PY)
        (d / "config.yaml").write_text("kind: indicator\n")
    else:
        (d / "plugin.py").write_text(STRATEGY_PY)
        (d / "config.yaml").write_text(
            "kind: strategy\ntimeframe: 1d\npairs: [USDJPY]\n"
            "exit_mode: levels\nmax_bars: 200\n")
    (d / "test_plugin.py").write_text(TEST_PY_OK)
    return d


def _meta(d: Path, *, kind: str, name: str) -> PluginMeta:
    if kind == "indicator":
        return PluginMeta(name=name, kind="indicator", path=d, params={},
                          timeframe=None, pairs=(), max_bars=200,
                          content_hash=real_content_hash(d))
    return PluginMeta(name=name, kind="strategy", path=d, params={},
                      timeframe="1d", pairs=("USDJPY",), max_bars=200,
                      content_hash=real_content_hash(d))


def _ok_pytest_runner(_path: Path) -> GateResult:
    return GateResult(passed=True, returncode=0,
                      stdout_tail="1 passed in 0.01s", duration_sec=0.01)


def _fake_pytest_ok(plugin_dir, *, settings):
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed",
                      duration_sec=0.1)


def _fake_evaluable_gate(conn, meta, *, settings, now, record_fn=None,
                         floor_mode="enforce", **_unused_kwargs):
    return StrategyGateVerdict(
        evaluable=True, baseline_variant="no_strategy",
        baseline_row={"plugin_ref": f"no_strategy:{meta.name}",
                     "variant": "no_strategy"},
        candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.2}})


def _fake_run_in_sample(settings_arg, **kwargs):
    return {"trades": 0, "pf": None, "win_rate": None, "avg_r": None,
            "max_drawdown": 0.0, "total_pnl": 0.0, "evaluable": False,
            "fallback_spread_used": False}


def _payload_from_submit_plugin(tmp_path, kind):
    d = _write_plugin(tmp_path / "a", f"{kind}_a", kind=kind)
    meta = _meta(d, kind=kind, name=f"{kind}_a")
    conn = _conn(tmp_path / "a")
    approval_id = approval.submit_plugin(
        conn, meta, settings=SETTINGS, now=NOW, pytest_runner=_ok_pytest_runner,
        run_in_sample_fn=_fake_run_in_sample)
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    return json.loads(row["payload_json"])


def _payload_from_switch_submit(tmp_path, kind, monkeypatch):
    root = tmp_path / "b"
    plugins_dir = root / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    conn = _conn(root)
    staging = plugins_dir / "_staging" / "1"
    _write_plugin(staging, f"{kind}_b", kind=kind)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    if kind == "strategy":
        monkeypatch.setattr(
            "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
            _fake_evaluable_gate)
    approval_id = switch.submit_candidate(
        conn, name=f"{kind}_b", staging_dir=staging,
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=SETTINGS, now=NOW)
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    return json.loads(row["payload_json"])


def _payload_from_switch_bless(tmp_path, kind, monkeypatch):
    root = tmp_path / "c"
    plugins_dir = root / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    conn = _conn(root)
    human_dir = plugins_dir / "_human" / f"{kind}_c"
    _write_plugin(plugins_dir / "_human", f"{kind}_c", kind=kind)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    if kind == "strategy":
        monkeypatch.setattr(
            "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
            _fake_evaluable_gate)
    approval_id = switch.bless_candidate(
        conn, name=f"{kind}_c", human_dir=human_dir, settings=SETTINGS, now=NOW,
        decided_by="human_cli")
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    return json.loads(row["payload_json"])


class _FakeRag:
    """`ImproveLoop.__init__(rag=...)` を満たすだけの no-op。"""


def _payload_from_improve_loop(tmp_path, kind):
    root = tmp_path / "d"
    root.mkdir()
    conn = _conn(root)
    loop = ImproveLoop(
        root=root, settings=SETTINGS, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: conn, db_readonly_conn_factory=lambda: conn,
        activity=ActivityLog(root / "activity.log"), rag=_FakeRag())
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()

    class _Meta:
        timeframe = "1d"

    gate_metrics = {"meta": _Meta()} if kind == "strategy" else {}
    return loop._build_approval_payload(
        conn, name=f"{kind}_d", kind=kind, content_hash="h", artifact_hash="a",
        ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path=f"plugins/_staging/1/{kind}_d",
        gate_metrics=gate_metrics, output={"selection_rationale": ""}, now=NOW)


@pytest.mark.parametrize("kind", ["strategy", "indicator"])
def test_four_payload_systems_share_eval_contract(tmp_path, monkeypatch, kind):
    # [profitability-floor] T1 Step 1-7 (2026-09-13、codex C1): legacy
    # `submit_plugin` は strategy candidate を fail closed で拒否する
    # ようになった (固定 holdout を含む共有ゲートを経由しない corridor
    # にフロアを一切課さない抜け道を塞ぐ、pin は
    # tests/plugin/test_approval.py::test_submit_plugin_rejects_strategy_
    # kind_f6_5) — kind="strategy" のときは 4 系統ではなく残り 3 系統
    # (switch.submit_candidate/switch.bless_candidate/improve_loop) だけ
    # を比較する。indicator は従来どおり 4 系統とも比較する。
    payloads = {
        "switch.submit_candidate": _payload_from_switch_submit(
            tmp_path, kind, monkeypatch),
        "switch.bless_candidate": _payload_from_switch_bless(
            tmp_path, kind, monkeypatch),
        "improve_loop._build_approval_payload": _payload_from_improve_loop(
            tmp_path, kind),
    }
    if kind != "strategy":
        payloads["submit_plugin"] = _payload_from_submit_plugin(tmp_path, kind)

    if kind == "strategy":
        expected = {"eval_source": "mt5", "base_interval": "5m",
                    "eval_timeframe": "24h",  # "1d" → "24h" 写像後
                    "live_source": "twelvedata"}
    else:
        expected = {"eval_source": None, "base_interval": None,
                    "eval_timeframe": None, "live_source": "twelvedata"}

    for system, payload in payloads.items():
        # 完全キー集合: 4 キーとも必ず存在する (欠落キーは契約違反)。
        assert _CONTRACT_KEYS.issubset(payload.keys()), (
            f"{system}: {_CONTRACT_KEYS - payload.keys()} キーが欠落")
        actual = {k: payload[k] for k in _CONTRACT_KEYS}
        assert actual == expected, f"{system}: {actual} != {expected}"

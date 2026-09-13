"""[profitability-floor] T1 Step 1-4〜1-8 (2026-09-13、設計書 §3 T1-d/e/
f/g、plan Step 1-9 の F6 系 pin)。`GateOutcome` 判別子・`_run_full_gate`
の raise/続行決定・`submit_candidate`/`bless_candidate` のフロア配線を
switch.py レベルで検証する。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.plugin import approval, strategy_gate, switch
from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)

STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
              "    return {'action': 'hold', 'rationale': 'x'}\n")
TEST_PY_OK = "def test_x():\n    pass\n"


def _write_strategy_candidate(dirpath: Path) -> None:
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(STRATEGY_PY)
    (dirpath / "config.yaml").write_text(
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n")
    (dirpath / "test_plugin.py").write_text(TEST_PY_OK)


@pytest.fixture
def env(tmp_path):
    root = tmp_path
    plugins_dir = root / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(root / "agentic.db")
    db_store.init_db(conn)
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    return root, plugins_dir, conn, settings


def _fake_pytest_ok(plugin_dir, *, settings):
    from agentic_fx.plugin.gate_pytest import GateResult
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed",
                      duration_sec=0.1)


def _floor_fail_gate(conn, *, meta, settings, now, floor_mode="enforce",
                     record_fn=None):
    """`evaluate_strategy_adoption_gate` のフェイク: 収益性フロア不合格
    (候補は評価可能、`floor_reason` が立つ)。record_fn へ 1 行積む
    (gate_rows 捕捉の確認用)。"""
    if record_fn is not None:
        record_fn({
            "scope": "in_sample", "plugin_ref": f"plugins/{meta.name}",
            "content_hash": meta.content_hash, "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "dukascopy",
            "base_interval": "1m", "params": {},
            "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                      datetime(2026, 2, 1, tzinfo=timezone.utc)),
            "metrics": {"trades": 40, "pf": 0.5, "win_rate": 0.4,
                       "avg_r": -0.1, "max_drawdown": 0.1, "total_pnl": -50.0,
                       "evaluable": True, "fallback_spread_used": False},
            "settings_hash": "sh1", "core_commit": "cc1",
            "initial_balance": 10000.0, "now": now, "variant": "candidate"})
    return StrategyGateVerdict(
        evaluable=True, baseline_variant="no_strategy",
        baseline_row={"plugin_ref": f"no_strategy:{meta.name}",
                     "variant": "no_strategy"},
        candidate_metrics={"USDJPY": {"trades": 40, "pf": 0.5, "avg_r": -0.1,
                                      "evaluable": True}},
        floor_reason="unprofitable", floor_detail="in_sample: USDJPY: pf=0.5")


def _insufficient_trades_gate(conn, *, meta, settings, now, floor_mode="enforce",
                              record_fn=None):
    """`evaluate_strategy_adoption_gate` のフェイク: 標本不足
    (`evaluable=False`)。**observation_reason に `"unprofitable"` という
    語を混ぜる** — pin F6-10 (文字列部分一致による誤分類が無いことの
    確認材料)。record_fn へも 1 行積み、実際に persist されるときの
    `mission_outcome` を検証できるようにする。"""
    if record_fn is not None:
        record_fn({
            "scope": "in_sample", "plugin_ref": f"plugins/{meta.name}",
            "content_hash": meta.content_hash, "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "dukascopy",
            "base_interval": "1m", "params": {},
            "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                      datetime(2026, 2, 1, tzinfo=timezone.utc)),
            "metrics": {"trades": 5, "pf": None, "win_rate": None,
                       "avg_r": None, "max_drawdown": 0.0, "total_pnl": 0.0,
                       "evaluable": False, "fallback_spread_used": False},
            "settings_hash": "sh1", "core_commit": "cc1",
            "initial_balance": 10000.0, "now": now, "variant": "candidate"})
    return StrategyGateVerdict(
        evaluable=False,
        observation_reason="insufficient_trades:5 (note: unprofitable-ish)")


def _ok_gate(conn, *, meta, settings, now, floor_mode="enforce", record_fn=None):
    return StrategyGateVerdict(
        evaluable=True, baseline_variant="no_strategy",
        baseline_row={"plugin_ref": f"no_strategy:{meta.name}",
                     "variant": "no_strategy"},
        candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.1,
                                      "evaluable": True}})


# ---- F6-9: run_kind_gate は決してゲート判定で例外を投げない ------------

def test_f6_9_run_kind_gate_never_raises_for_insufficient_trades(
        env, monkeypatch):
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    from agentic_fx.plugin.loader import _discover_one
    meta = _discover_one(d, "st")
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _insufficient_trades_gate)

    outcome = approval.run_kind_gate(conn, meta, settings=settings, now=NOW)

    assert isinstance(outcome, approval.GateOutcome)
    assert outcome.verdict_kind == "insufficient_trades"
    assert outcome.evaluable is False


def test_f6_9_run_kind_gate_never_raises_for_floor_failure(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    from agentic_fx.plugin.loader import _discover_one
    meta = _discover_one(d, "st")
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _floor_fail_gate)

    outcome = approval.run_kind_gate(conn, meta, settings=settings, now=NOW,
                                     floor_mode="enforce")

    assert isinstance(outcome, approval.GateOutcome)
    assert outcome.verdict_kind == "floor"
    assert outcome.floor_failed is True
    assert outcome.floor_warning == "unprofitable"


# ---- F6-10: 分類は verdict_kind のみ (文字列部分一致で分類しない) -------

def test_f6_10_insufficient_trades_reason_containing_unprofitable_word_is_not_misclassified(
        env, monkeypatch):
    """`observation_reason` に `"unprofitable"` という語が混ざっていても、
    `_run_full_gate` は `verdict_kind` だけを見て `gate_failed` として
    扱う (`unprofitable` として保存しない)。"""
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _insufficient_trades_gate)

    with pytest.raises(ValueError, match="not evaluable"):
        switch.submit_candidate(
            conn, name="st", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs").fetchone()
    assert row is not None
    assert row["mission_outcome"] == "gate_failed"  # "unprofitable" にしない


# ---- F6-1/F6-2: submit_candidate の enforce フロア不合格 ---------------

def test_f6_1_submit_candidate_floor_failure_raises_and_persists_unprofitable(
        env, monkeypatch):
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _floor_fail_gate)

    with pytest.raises(ValueError, match="unprofitable"):
        switch.submit_candidate(
            conn, name="st", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    assert approvals_store.pending(conn, kind="plugin") == []
    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs").fetchone()
    assert row is not None
    assert row["mission_outcome"] == "unprofitable"  # F6-8: None にしない


# ---- F6-11(a): submit_candidate の activity 配線 -----------------------

def test_f6_11a_submit_candidate_writes_submit_floor_rejected_activity(
        env, monkeypatch):
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _floor_fail_gate)
    activity = ActivityLog(root / "logs" / "activity.log")

    with pytest.raises(ValueError, match="unprofitable"):
        switch.submit_candidate(
            conn, name="st", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW, activity=activity)

    log_text = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "submit_floor_rejected" in log_text
    assert f"min_pf={settings.improve.gate.min_pf}" in log_text
    assert (f"require_positive_avg_r={settings.improve.gate.require_positive_avg_r}"
           in log_text)
    assert (f"require_holdout_evaluable="
           f"{settings.improve.gate.require_holdout_evaluable}" in log_text)


def test_f6_11a_backward_compat_activity_none_does_not_crash(env, monkeypatch):
    """`activity=None` (既定・後方互換): 例外にせず何もしない。"""
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _floor_fail_gate)

    with pytest.raises(ValueError, match="unprofitable"):
        switch.submit_candidate(
            conn, name="st", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)  # activity 省略


# ---- F6-11(b): CLI 配線 spy pin -----------------------------------------

def test_f6_11b_cli_plugin_submit_from_human_passes_root_logs_activity_log(
        tmp_path, monkeypatch):
    """codex R3-I2: (a) だけでは CLI が `activity=` を渡し忘れても検出
    できない — `_plugin_submit --from _human` を実行し、
    `submit_candidate` に渡された `activity` の `path` が
    `root/"logs"/"activity.log"` であることを直接 pin する。"""
    import argparse

    from agentic_fx.backtest import cli as backtest_cli

    root = tmp_path
    (root / "config").mkdir(parents=True)
    import shutil
    shutil.copy(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example",
        root / "config" / "settings.yaml")
    from agentic_fx.config import load_settings
    settings = load_settings(root / "config" / "settings.yaml")
    conn = db_store.connect(root / "data" / "agentic.db")
    db_store.init_db(conn)

    seen_activity_paths = []

    def _spy_submit_candidate(conn_arg, *, name, staging_dir, candidate_origin,
                              mission_id, backlog_id, settings, now,
                              activity=None):
        seen_activity_paths.append(
            activity._path if activity is not None else None)
        return 1

    monkeypatch.setattr(
        "agentic_fx.plugin.switch.submit_candidate", _spy_submit_candidate)
    monkeypatch.setattr(backtest_cli, "plugin_switch",
                        __import__("agentic_fx.plugin.switch",
                                   fromlist=["submit_candidate"]))

    args = argparse.Namespace(name="st", from_kind="_human")
    rc = backtest_cli._plugin_submit(conn, settings, args, root)

    assert rc == 0
    assert seen_activity_paths == [root / "logs" / "activity.log"]


# ---- F6-12: run_kind_gate を抜けてくる想定外例外 ------------------------

def test_f6_12_unexpected_exception_persists_gate_failed_rows_and_reraises(
        env, monkeypatch):
    """`record_fn` に行を積んだ後で `evaluate_strategy_adoption_gate` が
    想定外の例外 (holdout.NoHistoryError 相当) を投げても、捕捉済みの行が
    `gate_failed` で保存されてから元例外が伝播する (`unprofitable` で
    保存しない/行を捨てない)。"""
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    class _BoomError(RuntimeError):
        pass

    def _boom_gate(conn, *, meta, settings, now, floor_mode="enforce",
                  record_fn=None):
        if record_fn is not None:
            record_fn({
                "scope": "in_sample", "plugin_ref": f"plugins/{meta.name}",
                "content_hash": meta.content_hash, "kind": "strategy",
                "pair": "USDJPY", "timeframe": "1h", "source": "dukascopy",
                "base_interval": "1m", "params": {},
                "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                          datetime(2026, 2, 1, tzinfo=timezone.utc)),
                "metrics": {"trades": 40, "pf": None, "win_rate": None,
                           "avg_r": None, "max_drawdown": 0.0,
                           "total_pnl": 0.0, "evaluable": True,
                           "fallback_spread_used": False},
                "settings_hash": "sh1", "core_commit": "cc1",
                "initial_balance": 10000.0, "now": now, "variant": "candidate"})
        raise _BoomError("unprofitable-sounding candidate name should not "
                         "confuse classification: st")

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _boom_gate)

    with pytest.raises(_BoomError):
        switch.submit_candidate(
            conn, name="st", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs").fetchone()
    assert row is not None
    assert row["mission_outcome"] == "gate_failed"  # unprofitable にしない


# ---- F6-3/F6-7: bless_candidate は warn で完走・int を返す --------------

def test_f6_3_bless_candidate_floor_warning_completes_and_returns_int(
        env, monkeypatch):
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_human" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _floor_fail_gate)

    warnings_seen = []

    def _on_warn(label, detail):
        warnings_seen.append((label, detail))

    activity = ActivityLog(root / "logs" / "activity.log")
    approval_id = switch.bless_candidate(
        conn, name="st", human_dir=d, settings=settings, now=NOW,
        decided_by="human_cli", on_floor_warning=_on_warn, activity=activity)

    assert isinstance(approval_id, int)  # F6-7: 戻り値は int のまま
    assert warnings_seen == [("unprofitable", "in_sample: USDJPY: pf=0.5")]
    log_text = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "bless_floor_warning" in log_text
    row = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    import json
    payload = json.loads(row["payload_json"])
    assert payload["floor_warning"] == "unprofitable"
    assert payload["profitability_floor"]["min_pf"] == settings.improve.gate.min_pf


# ---- F6-4: CLI `_plugin_bless` — on_floor_warning=None は例外にしない/
# 実 CLI 経路は終了コード 0・stderr に警告 --------------------------------

def test_f6_4_bless_candidate_on_floor_warning_none_does_not_crash(
        env, monkeypatch):
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_human" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _floor_fail_gate)

    approval_id = switch.bless_candidate(
        conn, name="st", human_dir=d, settings=settings, now=NOW,
        decided_by="human_cli")  # on_floor_warning/activity とも省略

    assert isinstance(approval_id, int)


def test_f6_4_cli_plugin_bless_floor_warning_exits_zero_and_warns_stderr(
        tmp_path, monkeypatch, capsys):
    """`backtest/cli.py::_plugin_bless` はフロア不合格でも終了コード 0
    のまま、stderr に警告文を出す (人間裁定で承認は成立している)。"""
    import argparse

    from agentic_fx.backtest import cli as backtest_cli
    from agentic_fx.config import load_settings

    root = tmp_path
    (root / "config").mkdir(parents=True)
    import shutil
    shutil.copy(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example",
        root / "config" / "settings.yaml")
    settings = load_settings(root / "config" / "settings.yaml")
    conn = db_store.connect(root / "data" / "agentic.db")
    db_store.init_db(conn)

    def _fake_bless_candidate(conn_arg, *, name, human_dir, settings,
                              now, decided_by, on_floor_warning=None,
                              activity=None):
        if on_floor_warning is not None:
            on_floor_warning("unprofitable", "in_sample: USDJPY: pf=0.5")
        return 42

    monkeypatch.setattr(
        "agentic_fx.plugin.switch.bless_candidate", _fake_bless_candidate)

    args = argparse.Namespace(name="st", from_kind="_human")
    rc = backtest_cli._plugin_bless(conn, settings, args, root)

    assert rc == 0
    captured = capsys.readouterr()
    assert "警告" in captured.err
    assert "収益性フロア" in captured.err
    assert "approval <id>" in captured.err


# ---- 段0 是正 G3 (2026-09-13、指揮者独立変異 M8): run_kind_gate → -----
# evaluator の floor_mode 転送の pin (M8 は広域 1924 passed で生存していた)

def test_g3_run_kind_gate_forwards_floor_mode_warn_to_evaluator(env, monkeypatch):
    """(a) `run_kind_gate(..., floor_mode="warn")` を呼ぶと、
    `evaluate_strategy_adoption_gate` に `floor_mode="warn"` が実際に
    届く。逆変異: `run_kind_gate` から `floor_mode=floor_mode` の転送を
    外す → killer。"""
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    from agentic_fx.plugin.loader import _discover_one
    meta = _discover_one(d, "st")

    seen_floor_modes = []

    def _spy(conn_arg, *, meta, settings, now, floor_mode="enforce",
            record_fn=None):
        seen_floor_modes.append(floor_mode)
        return StrategyGateVerdict(
            evaluable=True, baseline_variant="no_strategy",
            baseline_row={"plugin_ref": f"no_strategy:{meta.name}",
                         "variant": "no_strategy"},
            candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.5,
                                          "avg_r": 0.1, "evaluable": True}})

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _spy)

    approval.run_kind_gate(conn, meta, settings=settings, now=NOW,
                           floor_mode="warn")

    assert seen_floor_modes == ["warn"]


def test_g3_run_kind_gate_default_floor_mode_is_enforce(env, monkeypatch):
    """(b) `floor_mode` を明示しない既定呼び出しでは `"enforce"` が届く。"""
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(d)
    from agentic_fx.plugin.loader import _discover_one
    meta = _discover_one(d, "st")

    seen_floor_modes = []

    def _spy(conn_arg, *, meta, settings, now, floor_mode="enforce",
            record_fn=None):
        seen_floor_modes.append(floor_mode)
        return StrategyGateVerdict(
            evaluable=True, baseline_variant="no_strategy",
            baseline_row={"plugin_ref": f"no_strategy:{meta.name}",
                         "variant": "no_strategy"},
            candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.5,
                                          "avg_r": 0.1, "evaluable": True}})

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _spy)

    approval.run_kind_gate(conn, meta, settings=settings, now=NOW)

    assert seen_floor_modes == ["enforce"]


def test_g3_bless_candidate_warn_reaches_holdout_seam_via_real_evaluator(
        env, monkeypatch):
    """(c) codex C2 の保証を switch 層から通しで確認する:
    `evaluate_strategy_adoption_gate` は fake せず (実 evaluator を通す)、
    `holdout.run_in_sample`/`holdout.run_holdout_gate` の seam だけを
    fake する。in_sample 段がフロア不合格でも、`bless_candidate` の
    `floor_mode="warn"` により `run_holdout_gate` seam に実際に到達する
    (= run_kind_gate → evaluator への floor_mode 転送が生きている証拠)。
    逆変異: `run_kind_gate` の `floor_mode=floor_mode` 転送を外す →
    evaluator は既定 `enforce` になり in_sample で短絡、
    `run_holdout_gate` seam に到達しなくなる → killer。"""
    root, plugins_dir, conn, settings = env
    d = plugins_dir / "_human" / "st"
    _write_strategy_candidate(d)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: type("Fake", (), {"close": lambda self: None})())

    holdout_calls = []

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        return {"trades": 40, "pf": 0.3, "avg_r": -0.2, "win_rate": 0.4,
                "max_drawdown": 0.1, "total_pnl": -50.0, "evaluable": True,
                "fallback_spread_used": False}

    def _fake_run_holdout_gate(*a, record_fn=None, **kw):
        holdout_calls.append(1)
        return {"trades": 40, "pf": 1.5, "avg_r": 0.1, "win_rate": 0.5,
                "max_drawdown": 0.05, "total_pnl": 100.0, "evaluable": True,
                "fallback_spread_used": False}

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        _fake_run_holdout_gate)

    switch.bless_candidate(
        conn, name="st", human_dir=d, settings=settings, now=NOW,
        decided_by="human_cli")

    assert holdout_calls == [1]  # warn 経路: in_sample 不合格でも holdout へ到達

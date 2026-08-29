"""round2 #2 是正 (verified-round2.md #2): `max_bars_limit` ゲートが
`switch._run_full_gate` (P1 submit / P3 bless の共有ゲート本体、プラン10で
新設された2本目の承認 corridor) に無かった。F1 (最終レビュー) が
`approval.submit_plugin` に足したゲートは当時唯一の corridor だったが、
本 corridor には付いていなかった。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import approval, switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)

INDICATOR_PY = "def compute(df, params):\n    return {'v': 1.0}\n"
TEST_PY_OK = "def test_x():\n    pass\n"


def _write_candidate(dirpath: Path, *, max_bars: int | None) -> None:
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(INDICATOR_PY)
    config = "kind: indicator\n"
    if max_bars is not None:
        config += f"max_bars: {max_bars}\n"
    (dirpath / "config.yaml").write_text(config)
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


def test_full_gate_rejects_oversized_max_bars(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    assert settings.plugin.max_bars_limit < 500000
    _write_candidate(plugins_dir / "_staging" / "1" / "sma", max_bars=500000)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    with pytest.raises(ValueError, match="max_bars_limit"):
        switch.submit_candidate(
            conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    # 手順 7 (run_kind_gate) に入る前に落ちることを固定する — 例外が出る
    # だけでは手順7の後ろに置く変異を殺せない。
    assert approvals_store.pending(conn, kind="plugin") == []
    backtest_rows = conn.execute(
        "SELECT count(*) c FROM backtest_runs").fetchone()["c"]
    assert backtest_rows == 0


def test_full_gate_accepts_max_bars_within_limit(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma",
                     max_bars=settings.plugin.max_bars_limit)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    assert approval_id is not None
    assert len(approvals_store.pending(conn, kind="plugin")) == 1


# round2 最終是正 A3 (2026-08-29、verified-local-round2.md A3):
# `test_full_gate_rejects_oversized_max_bars` の `backtest_runs == 0` は
# `kind: indicator` しか流していないため、順序 (`assert_max_bars_within_limit`
# を `run_kind_gate` より前に置く) を壊す変異を殺せない — indicator は
# `run_kind_gate` 自体がバックテストを走らせないので、順序を壊しても
# `backtest_runs == 0` は成立してしまう。`run_kind_gate` に spy を挟み、
# 手順7 に一切入らないことを直接固定する (strategy 候補を作る必要はない —
# spy を挟んだ時点で kind は結論に効かない)。
def test_full_gate_rejects_oversized_max_bars_before_the_kind_gate(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    assert settings.plugin.max_bars_limit < 500000
    _write_candidate(plugins_dir / "_staging" / "1" / "sma", max_bars=500000)
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    calls: list[int] = []
    monkeypatch.setattr(
        approval, "run_kind_gate",
        lambda *a, **kw: (calls.append(1), ({}, True))[1])

    with pytest.raises(ValueError, match="max_bars_limit"):
        switch.submit_candidate(
            conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    assert calls == []  # ← 手順7 (run_kind_gate) に一切入らない = 移動変異を殺す

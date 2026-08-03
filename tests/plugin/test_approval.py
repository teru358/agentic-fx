"""approval.py (submit_plugin / bless) のテスト (プラン 7 Task 6)。

fake pytest_runner/sandbox_run/run_in_sample_fn を注入する経路は実サブ
プロセスを一切起動しない。実サブプロセスは統合テスト 2 本のみ
(①既定 pytest_runner の実サブプロセス実行 ②strategy 経路の実
PluginSession + 実 holdout.run_in_sample — 実行時間の実測記録が目的)。
CLI 配線テストは entry.main([...]) 経由で `agentic_fx.plugin.approval.
submit_plugin`/`bless` が実際に呼ばれることを検証する (「単体が緑でも
誰からも呼ばれない」を防ぐ — service.run_service も併せて patch し、
配線漏れがあってもテストが service 起動でハングしないようにする)。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.backtest.holdout import holdout_boundary
from agentic_fx.config import load_settings
from agentic_fx.entry import main
from agentic_fx.plugin import approval
from agentic_fx.plugin.loader import PluginMeta, content_hash as real_content_hash
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import ohlcv as ohlcv_store
from agentic_fx.store.db import connect, init_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.yaml.example"
_SMA_CROSS_DIR = _REPO_ROOT / "docs" / "examples" / "plugins" / "sma_cross"

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

INDICATOR_PY = """
def compute(df, params):
    return {"v": 1.0}
"""

STRATEGY_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "noop"}
"""

SIGNAL_PY = """
def detect(df, params):
    return []
"""

TEST_PY_OK = """
def test_placeholder():
    pass
"""

TEST_PY_IMPORT_OS = """
import os


def test_placeholder():
    pass
"""


@pytest.fixture(scope="module")
def settings():
    return load_settings(_EXAMPLE)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _write_plugin(base: Path, name: str, *, kind: str, plugin_py: str,
                  config_yaml: str, test_py: str = TEST_PY_OK) -> Path:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(test_py)
    return d


def _indicator_meta(d: Path, name: str = "ind") -> PluginMeta:
    return PluginMeta(name=name, kind="indicator", path=d, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=real_content_hash(d))


def _strategy_meta(d: Path, *, name: str = "strat",
                   pairs: tuple[str, ...] = ("USDJPY",),
                   timeframe: str = "1h") -> PluginMeta:
    return PluginMeta(name=name, kind="strategy", path=d, params={},
                      timeframe=timeframe, pairs=pairs, max_bars=200,
                      content_hash=real_content_hash(d))


def _signal_meta(d: Path, *, name: str = "sig") -> PluginMeta:
    return PluginMeta(name=name, kind="signal", path=d, params={},
                      timeframe="1h", pairs=("USDJPY",), max_bars=200,
                      content_hash=real_content_hash(d))


def _ok_pytest_runner(_path: Path) -> dict:
    return {"returncode": 0, "stdout": "1 passed in 0.01s"}


def _fail_pytest_runner(_path: Path) -> dict:
    return {"returncode": 1, "stdout": "1 failed in 0.01s"}


def _count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


# --- ① pytest 失敗で行ができない --------------------------------------

def test_pytest_failure_raises_value_error_and_creates_no_row(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)

    with pytest.raises(ValueError, match="failed pytest"):
        approval.submit_plugin(_conn(tmp_path), meta, settings=settings, now=NOW,
                               pytest_runner=_fail_pytest_runner)


def test_pytest_failure_creates_no_approval_row(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_fail_pytest_runner)
    assert _count_rows(conn) == 0


# --- ② indicator 成功で行 + content_hash/test_file_hash ---------------

def test_indicator_success_creates_row_with_expected_payload(tmp_path, settings):
    import hashlib

    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)

    approval_id = approval.submit_plugin(
        conn, meta, settings=settings, now=NOW, pytest_runner=_ok_pytest_runner)

    assert isinstance(approval_id, int)
    row = conn.execute(
        "SELECT kind, status, payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    assert row["kind"] == "plugin"
    assert row["status"] == "pending"

    import json
    payload = json.loads(row["payload_json"])
    assert payload["name"] == "ind"
    assert payload["kind"] == "indicator"
    assert payload["content_hash"] == meta.content_hash
    assert payload["test_file_hash"] == hashlib.sha256(
        (d / "test_plugin.py").read_bytes()).hexdigest()
    assert payload["pytest"] == {"returncode": 0, "summary": "1 passed in 0.01s"}
    assert payload["metrics"] == {}
    assert payload["evaluable"] is True
    assert payload["eval_source"] == "dukascopy"
    assert payload["live_source"] == settings.plugin.producer_source
    assert payload["note"] == "バックテスト成績は実運用成績の予測値ではない (足切り専用)"


# --- signal: sandbox_run 経由で evaluate_detection が呼ばれる ------------

def test_signal_metrics_come_from_evaluate_detection(tmp_path, settings):
    import json

    d = _write_plugin(tmp_path, "sig", kind="signal", plugin_py=SIGNAL_PY,
                      config_yaml="kind: signal\ntimeframe: 1h\npairs: [USDJPY]\n")
    bars = [["2026-01-01T00:00:00Z", 100.0, 100.5, 99.5, 100.2, 10.0],
           ["2026-01-01T01:00:00Z", 100.2, 100.6, 99.8, 100.3, 10.0]]
    expected = [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}]
    (d / "labels.json").write_text(json.dumps({"bars": bars, "expected": expected}))
    meta = _signal_meta(d)
    conn = _conn(tmp_path)

    def fake_sandbox_run(meta_arg, payload, *, settings):
        latest = payload["df"].index[-1]
        if latest.isoformat() == "2026-01-01T00:00:00+00:00":
            return {"signals": [{"direction": "long", "strength": 0.9, "rationale": "r"}]}
        return {"signals": []}

    approval_id = approval.submit_plugin(
        conn, meta, settings=settings, now=NOW, pytest_runner=_ok_pytest_runner,
        sandbox_run=fake_sandbox_run)

    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["payload_json"])
    assert payload["metrics"]["precision"] == pytest.approx(1.0)
    assert payload["metrics"]["recall"] == pytest.approx(1.0)
    assert payload["evaluable"] is True


# --- ③ strategy: pairs 毎に run_in_sample_fn が実シグネチャ相当で呼ばれる --

def test_strategy_calls_run_in_sample_fn_per_pair_with_expected_kwargs(
        tmp_path, settings):
    d = _write_plugin(tmp_path, "strat", kind="strategy", plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1h\n"
                                 "pairs: [USDJPY, EURUSD]\nexit_mode: levels\n"
                                 "max_bars: 200\n")
    meta = _strategy_meta(d, pairs=("USDJPY", "EURUSD"))
    two_pair_settings = settings.model_copy(update={"pairs": ["USDJPY", "EURUSD"]})
    conn = _conn(tmp_path)

    calls: list[dict] = []

    def fake_run_in_sample(settings_arg, *, history_conn, symbol, source,
                           intent_source, eval_timeframe, plugin_ref,
                           content_hash, kind, now):
        calls.append({
            "settings": settings_arg, "history_conn": history_conn,
            "symbol": symbol, "source": source, "intent_source": intent_source,
            "eval_timeframe": eval_timeframe, "plugin_ref": plugin_ref,
            "content_hash": content_hash, "kind": kind, "now": now})
        return {"trades": 15, "pf": 1.0, "win_rate": 0.5, "avg_r": 0.1,
                "max_drawdown": 0.05, "total_pnl": 100.0, "evaluable": False,
                "fallback_spread_used": False}

    pytest_calls: list[Path] = []

    def counting_ok_runner(path: Path) -> dict:
        pytest_calls.append(path)
        return _ok_pytest_runner(path)

    approval_id = approval.submit_plugin(
        conn, meta, settings=two_pair_settings, now=NOW,
        pytest_runner=counting_ok_runner, run_in_sample_fn=fake_run_in_sample)

    assert len(pytest_calls) == 1  # pytest はプラグイン全体で 1 回だけ
    assert len(calls) == 2
    symbols = {c["symbol"] for c in calls}
    assert symbols == {"USDJPY", "EURUSD"}
    for c in calls:
        assert c["source"] == "dukascopy"
        assert c["eval_timeframe"] == "1h"
        assert c["plugin_ref"] == "plugins/strat"
        assert c["content_hash"] == meta.content_hash
        assert c["kind"] == "strategy"
        assert c["now"] == NOW
        assert c["history_conn"] is conn

    import json
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["payload_json"])
    assert set(payload["metrics"]) == {"USDJPY", "EURUSD"}
    assert payload["metrics"]["USDJPY"]["trades"] == 15
    # 15+15=30 == EVALUABLE_MIN_TRADES (境界値, >= なので True)
    assert payload["evaluable"] is True


def test_strategy_eval_timeframe_maps_1d_to_24h(tmp_path, settings):
    d = _write_plugin(tmp_path, "strat_1d", kind="strategy", plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1d\npairs: [USDJPY]\n"
                                 "exit_mode: levels\nmax_bars: 200\n")
    meta = _strategy_meta(d, name="strat_1d", timeframe="1d")
    conn = _conn(tmp_path)

    calls: list[dict] = []

    def fake_run_in_sample(settings_arg, **kwargs):
        calls.append(kwargs)
        return {"trades": 0, "pf": None, "win_rate": None, "avg_r": None,
                "max_drawdown": 0.0, "total_pnl": 0.0, "evaluable": False,
                "fallback_spread_used": False}

    approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                           pytest_runner=_ok_pytest_runner,
                           run_in_sample_fn=fake_run_in_sample)
    assert len(calls) == 1
    assert calls[0]["eval_timeframe"] == "24h"


# --- ④ pairs が settings.pairs 外 ValueError ----------------------------

def test_strategy_pair_outside_settings_pairs_raises_value_error(tmp_path, settings):
    d = _write_plugin(tmp_path, "strat_bad", kind="strategy", plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1h\npairs: [EURJPY]\n"
                                 "exit_mode: levels\nmax_bars: 200\n")
    meta = _strategy_meta(d, name="strat_bad", pairs=("EURJPY",))
    conn = _conn(tmp_path)

    called = []

    def fake_run_in_sample(settings_arg, **kwargs):
        called.append(kwargs)
        return {"trades": 0}

    with pytest.raises(ValueError, match="not in settings.pairs"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_ok_pytest_runner,
                               run_in_sample_fn=fake_run_in_sample)
    assert called == []  # 1 pair 目ですでに ValueError — バックテストは 0 回
    assert _count_rows(conn) == 0


# --- ⑤ test_plugin.py の import os reject ------------------------------

def test_test_plugin_py_import_os_is_rejected(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind_bad_test", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n",
                      test_py=TEST_PY_IMPORT_OS)
    meta = _indicator_meta(d, name="ind_bad_test")
    conn = _conn(tmp_path)

    with pytest.raises(ValueError, match="import of 'os' is not allowed"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_ok_pytest_runner)
    assert _count_rows(conn) == 0


def test_plugin_py_rejected_source_creates_no_row(tmp_path, settings):
    """①も同じ変換規約で ValueError になることを確認する (plugin.py 側)。"""
    d = _write_plugin(tmp_path, "ind_bad_plugin", kind="indicator",
                      plugin_py="import os\n\n\ndef compute(df, params):\n"
                                "    return {}\n",
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_bad_plugin")
    conn = _conn(tmp_path)

    with pytest.raises(ValueError, match="import of 'os' is not allowed"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_ok_pytest_runner)
    assert _count_rows(conn) == 0


# --- ⑥ bless 成功で approved 行 / 検証失敗で何も作らない -----------------

def test_bless_success_creates_approved_row(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)

    approval_id = approval.bless(conn, meta, settings=settings, now=NOW,
                                 pytest_runner=_ok_pytest_runner)

    row = conn.execute(
        "SELECT status, decided_by FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    assert row["status"] == "approved"
    assert row["decided_by"] == "human_cli"
    assert approvals_store.pending(conn) == []  # もう pending ではない


def test_bless_validation_failure_creates_no_row(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)

    with pytest.raises(ValueError):
        approval.bless(conn, meta, settings=settings, now=NOW,
                       pytest_runner=_fail_pytest_runner)
    assert _count_rows(conn) == 0


# --- 変異検知用: 検証順序 (pytest 前に check_source を通す) --------------

def test_plugin_py_check_source_runs_before_pytest(tmp_path, settings):
    """plugin.py が sandbox 拒否されるとき、pytest_runner は一度も呼ばれ
    ないこと (検証順序 ①→② のピン)。"""
    d = _write_plugin(tmp_path, "ind_bad_plugin2", kind="indicator",
                      plugin_py="import os\n\n\ndef compute(df, params):\n"
                                "    return {}\n",
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_bad_plugin2")
    conn = _conn(tmp_path)
    called = []

    def spy_runner(path):
        called.append(path)
        return _ok_pytest_runner(path)

    with pytest.raises(ValueError):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=spy_runner)
    assert called == []


# --- 統合テスト①: 既定 pytest_runner の実サブプロセス実行 ---------------

def test_integration_default_pytest_runner_real_subprocess(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind_real", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_real")
    conn = _conn(tmp_path)

    started = time.perf_counter()
    approval_id = approval.submit_plugin(conn, meta, settings=settings, now=NOW)
    elapsed = time.perf_counter() - started
    print(f"\n[Task 6 実測] indicator submit (実 pytest subprocess): "
         f"{elapsed:.3f}s")

    row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    assert row["status"] == "pending"


# --- 統合テスト②: strategy 経路の実 PluginSession + 実 run_in_sample -----

def _seed_flat(conn, start: datetime, minutes: int, *, price: float,
               source: str = "dukascopy") -> None:
    rows = [("USDJPY", "1m", (start + timedelta(minutes=i)).isoformat(),
             price, price, price, price, 10.0, 0.01) for i in range(minutes)]
    ohlcv_store.import_bars(conn, rows, source=source)


def test_integration_strategy_real_session_and_run_in_sample(tmp_path, settings):
    """実 pytest + 実 PluginSession (sma_cross サンプル) + 実
    holdout.run_in_sample を通す (fake 一切なし)。opus R2 I1 の性能懸念
    (セッション型で解消される見込み) を実測するための唯一の実サブプロセス
    strategy 統合テスト — 実測秒数は report に記録する。
    """
    boundary = holdout_boundary(NOW, settings.backtest.holdout_months)
    seed_start = boundary - timedelta(days=2)

    conn = _conn(tmp_path)
    _seed_flat(conn, seed_start, 2 * 24 * 60 + 1, price=150.0)

    meta = PluginMeta(
        name="sma_cross", kind="strategy", path=_SMA_CROSS_DIR,
        params={"fast_period": 5, "slow_period": 20, "stop_loss_pips": 20,
               "take_profit_pips": 40, "pip_size": 0.01},
        timeframe="1h", pairs=("USDJPY",), max_bars=200,
        content_hash=real_content_hash(_SMA_CROSS_DIR))

    started = time.perf_counter()
    approval_id = approval.submit_plugin(conn, meta, settings=settings, now=NOW)
    elapsed = time.perf_counter() - started
    print(f"\n[Task 6 実測] strategy submit (実 pytest + 実 PluginSession + "
         f"実 run_in_sample, 2 日分 1h 評価): {elapsed:.3f}s")

    import json
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["payload_json"])
    assert "USDJPY" in payload["metrics"]
    assert "trades" in payload["metrics"]["USDJPY"]
    assert payload["content_hash"] == meta.content_hash


# --- CLI 配線: entry.main(["plugin", "submit"/"bless", name]) ------------

def _install_settings(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(_EXAMPLE, root / "config" / "settings.yaml")


def _write_cli_indicator_plugin(plugins_dir: Path, name: str) -> Path:
    d = plugins_dir / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(INDICATOR_PY)
    (d / "config.yaml").write_text("kind: indicator\n")
    (d / "test_plugin.py").write_text(TEST_PY_OK)
    return d


def test_entry_plugin_submit_dispatches_to_approval_submit_plugin(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    _write_cli_indicator_plugin(tmp_path / "plugins", "ind")

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service") as run_service, \
         patch("agentic_fx.plugin.approval.submit_plugin") as submit_mock:
        submit_mock.return_value = 42
        rc = main(["plugin", "submit", "ind"])

    assert rc == 0
    run_service.assert_not_called()
    assert submit_mock.called
    args, kwargs = submit_mock.call_args
    assert args[1].name == "ind"
    assert "42" in capsys.readouterr().out


def test_entry_plugin_bless_dispatches_to_approval_bless(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    _write_cli_indicator_plugin(tmp_path / "plugins", "ind")

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service") as run_service, \
         patch("agentic_fx.plugin.approval.bless") as bless_mock:
        bless_mock.return_value = 7
        rc = main(["plugin", "bless", "ind"])

    assert rc == 0
    run_service.assert_not_called()
    assert bless_mock.called
    args, kwargs = bless_mock.call_args
    assert args[1].name == "ind"
    assert "7" in capsys.readouterr().out


def test_entry_plugin_submit_not_found_rc1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    (tmp_path / "plugins").mkdir()

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service") as run_service:
        rc = main(["plugin", "submit", "nope"])

    assert rc == 1
    run_service.assert_not_called()
    assert "エラー" in capsys.readouterr().err


def test_entry_plugin_submit_validation_failure_rc1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    _write_cli_indicator_plugin(tmp_path / "plugins", "ind")

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service") as run_service, \
         patch("agentic_fx.plugin.approval.submit_plugin") as submit_mock:
        submit_mock.side_effect = ValueError("boom")
        rc = main(["plugin", "submit", "ind"])

    assert rc == 1
    run_service.assert_not_called()
    assert "エラー" in capsys.readouterr().err

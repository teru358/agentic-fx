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

import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentic_fx.backtest.holdout import holdout_boundary
from agentic_fx.config import load_settings
from agentic_fx import entry
from agentic_fx.entry import main
from agentic_fx.plugin import approval
from agentic_fx.plugin.gate_pytest import GateResult
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


def _ok_pytest_runner(_path: Path) -> GateResult:
    return GateResult(passed=True, returncode=0,
                      stdout_tail="1 passed in 0.01s", duration_sec=0.01)


def _fail_pytest_runner(_path: Path) -> GateResult:
    return GateResult(passed=False, returncode=1,
                      stdout_tail="1 failed in 0.01s", duration_sec=0.01)


def _count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


# --- 最終レビュー F1 (Fable/codex 両方一致, Important): max_bars_limit ----


def test_submit_rejects_max_bars_over_limit_and_creates_no_row(tmp_path, settings):
    """`max_bars: 500000` のような plugin は、承認バックテストの毎バケッ
    トで全履歴読みを起こしうる (producer は資金保護処理より前に走るため
    tick 遅延の実害がある)。市場ツール (get_indicators) だけが照合してい
    た上限を、承認ゲート冒頭でも fail closed で照合する — 超過なら
    check_source/pytest に進む前に ValueError で行 0 件のまま終える。"""
    d = _write_plugin(tmp_path, "ind_huge", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = PluginMeta(name="ind_huge", kind="indicator", path=d, params={},
                      timeframe=None, pairs=(), max_bars=500_000,
                      content_hash=real_content_hash(d))
    conn = _conn(tmp_path)

    with pytest.raises(ValueError, match="max_bars_limit"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_ok_pytest_runner)
    assert _count_rows(conn) == 0


def test_submit_accepts_max_bars_at_limit_boundary(tmp_path, settings):
    """`max_bars == settings.plugin.max_bars_limit` は許容される境界
    (`>` 判定であって `>=` ではない)。"""
    d = _write_plugin(tmp_path, "ind_at_limit", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = PluginMeta(name="ind_at_limit", kind="indicator", path=d, params={},
                      timeframe=None, pairs=(),
                      max_bars=settings.plugin.max_bars_limit,
                      content_hash=real_content_hash(d))
    conn = _conn(tmp_path)

    approval_id = approval.submit_plugin(
        conn, meta, settings=settings, now=NOW, pytest_runner=_ok_pytest_runner)
    assert isinstance(approval_id, int)
    assert _count_rows(conn) == 1


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


# <!-- precheck 2026-08-22: T6-B1 --> fail closed: `GateResult.passed` を
# 読む (旧実装は `returncode` しか見ず、hash 不一致・timeout でも
# `returncode == 0` なら承認申請行を作っていた — fail open)。


def test_gate_hash_mismatch_creates_no_approval_row_even_when_returncode_zero(
        tmp_path, settings):
    """`run_gate_pytest` は候補 hash 不一致のとき `GateResult(passed=False,
    returncode=<pytest の実 returncode>)` を返す — テスト自体が緑なら
    returncode は 0。`submit_plugin` が `returncode` だけを見ていると
    この分岐を見落として承認申請行を作ってしまう (fail open) — 直接
    `passed=False, returncode=0` を注入して確認する (B1 主 pin)。"""
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)

    def hash_mismatch_runner(_path: Path) -> GateResult:
        return GateResult(passed=False, returncode=0,
                          stdout_tail="1 passed in 0.01s\n[gate_pytest] "
                                      "candidate content changed during "
                                      "test run (hash mismatch) — rejecting",
                          duration_sec=0.01)

    with pytest.raises(ValueError, match="failed pytest"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=hash_mismatch_runner)
    assert _count_rows(conn) == 0


def test_gate_hash_mismatch_fault_injection_via_default_runner_creates_no_row(
        tmp_path, settings, monkeypatch):
    """B1 副 killer (統合): 既定 pytest_runner (`run_gate_pytest`、実
    Landlock プロセス) を使い、`hashes_of` を fault injection して pytest
    実行の間に候補が改ざんされた状態を再現する (6-B′ の
    `test_run_gate_pytest_fails_when_hash_changes_between_before_and_after`
    と同じ手法)。`submit_plugin` を通したときに承認申請行が **0 件**の
    ままであること — `GateResult.passed` が本番経路 (`submit_plugin`)
    まで届いていることを検証する。"""
    from agentic_fx.core.landlock import is_available
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    d = _write_plugin(tmp_path, "ind_tamper", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_tamper")
    conn = _conn(tmp_path)

    import agentic_fx.plugin.gate_pytest as gate_mod
    real_hashes_of = gate_mod.hashes_of
    call_count = {"n": 0}

    def tampering_hashes_of(plugin_dir):
        call_count["n"] += 1
        if call_count["n"] == 2:  # after 呼び出しのタイミングで改ざんする
            (plugin_dir / "test_plugin.py").write_text(
                "def test_placeholder():\n    pass\n# tampered\n")
        return real_hashes_of(plugin_dir)

    monkeypatch.setattr(gate_mod, "hashes_of", tampering_hashes_of)

    with pytest.raises(ValueError, match="failed pytest"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW)
    assert _count_rows(conn) == 0, \
        "FAIL-OPEN: row created despite gate hash mismatch"


# --- ② indicator 成功で行 + content_hash/test_file_hash ---------------

def test_indicator_success_creates_row_with_expected_payload(tmp_path, settings):
    import hashlib

    settings = settings.model_copy(update={
        "backtest": settings.backtest.model_copy(update={"eval_source": "mt5"})})

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
    assert payload["eval_source"] == "mt5"
    assert payload["live_source"] == settings.plugin.producer_source
    assert payload["note"] == "バックテスト成績は実運用成績の予測値ではない (足切り専用)"


# --- F1 (レビュー fix round 1, codex Critical): content_hash 再検証 -------

def test_content_hash_tampered_during_verification_raises_value_error(
        tmp_path, settings):
    """stale hash swap シナリオ: 検証中 (pytest 実行のタイミング) に
    plugin.py の内容が書き換えられた場合、approvals.create 直前の
    content_hash 再検証がこれを検出し ValueError で行を作らないこと。
    discover 時に取得した meta.content_hash は書き換え前の内容のまま
    (payload に古いハッシュが載って tools/plugin_loader を騙す、という
    F1 の攻撃/事故シナリオそのものを再現する)。"""
    d = _write_plugin(tmp_path, "ind_toctou", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_toctou")
    conn = _conn(tmp_path)

    def tampering_pytest_runner(path: Path) -> GateResult:
        # 検証 (pytest 実行) のタイミングで plugin.py の内容を変える —
        # check_source/pytest はこの変更後の内容に対して実行される (=
        # 「良性版に差し替えて検証を通す」の代わりに単に内容を変えるだけ
        # でも discover 時のハッシュとズレることを示せば十分)。
        (d / "plugin.py").write_text(INDICATOR_PY + "\n# tampered after discover\n")
        return _ok_pytest_runner(path)

    with pytest.raises(ValueError, match="content changed since discovery"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=tampering_pytest_runner)
    assert _count_rows(conn) == 0


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
    two_pair_settings = settings.model_copy(update={
        "pairs": ["USDJPY", "EURUSD"],
        "backtest": settings.backtest.model_copy(update={"eval_source": "mt5"}),
    })
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

    def counting_ok_runner(path: Path) -> GateResult:
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
        assert c["source"] == "mt5"
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
    assert payload["eval_source"] == "mt5"
    # 15+15=30 == EVALUABLE_MIN_TRADES (境界値, >= なので True)
    assert payload["evaluable"] is True


def test_strategy_sandbox_error_from_run_in_sample_becomes_value_error(
        tmp_path, settings):
    """③ kind 別検証段階 (strategy の run_in_sample) で SandboxError が
    出た場合も submit_plugin の統一契約どおり ValueError に変換され、
    approval_requests 行は作られないこと (①/②の check_source 失敗経路
    しか通らない import-os テストでは検証できない分岐 — advisor 指摘)。"""
    from agentic_fx.plugin.sandbox import SandboxError

    d = _write_plugin(tmp_path, "strat_crash", kind="strategy",
                      plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1h\n"
                                 "pairs: [USDJPY]\nexit_mode: levels\n"
                                 "max_bars: 200\n")
    meta = _strategy_meta(d, name="strat_crash", pairs=("USDJPY",))
    conn = _conn(tmp_path)

    def crashing_run_in_sample(settings_arg, **kwargs):
        raise SandboxError("plugin worker crashed mid-evaluation")

    with pytest.raises(ValueError, match="plugin worker crashed mid-evaluation"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_ok_pytest_runner,
                               run_in_sample_fn=crashing_run_in_sample)
    assert _count_rows(conn) == 0


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


def test_strategy_second_pair_outside_settings_pairs_raises_value_error(
        tmp_path, settings):
    """F2 (レビュー fix round 1, sonnet 変異生存): pairs 検証を
    「先頭 pair のみ」に縮小する変異が既存テストでは検出できなかった
    (先頭が不正なケースしか無かったため)。先頭は settings.pairs 内の
    有効な pair ("USDJPY")・後方が settings.pairs 外 ("EURJPY") という
    組み合わせで、後方の不正 pair も確実に検出されることをピンする。"""
    d = _write_plugin(tmp_path, "strat_bad2", kind="strategy",
                      plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1h\n"
                                 "pairs: [USDJPY, EURJPY]\nexit_mode: levels\n"
                                 "max_bars: 200\n")
    meta = _strategy_meta(d, name="strat_bad2", pairs=("USDJPY", "EURJPY"))
    conn = _conn(tmp_path)  # settings.pairs は既定で ["USDJPY"] のみ

    called = []

    def fake_run_in_sample(settings_arg, **kwargs):
        called.append(kwargs)
        return {"trades": 0}

    with pytest.raises(ValueError, match="not in settings.pairs"):
        approval.submit_plugin(conn, meta, settings=settings, now=NOW,
                               pytest_runner=_ok_pytest_runner,
                               run_in_sample_fn=fake_run_in_sample)
    assert called == []  # 先頭 pair が有効でも、後方の不正で 0 回のまま
    assert _count_rows(conn) == 0


# --- F3 (レビュー fix round 1, sonnet 変異生存): intent_source.close() ----


class _FakeIntentSource:
    """close() 呼び出しを観測するためだけの fake アダプタ
    (strategy_adapter.build_intent_source を丸ごと差し替える)。"""

    def __init__(self, pair: str) -> None:
        self.pair = pair
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_strategy_close_called_for_each_pair_on_success(tmp_path, settings):
    d = _write_plugin(tmp_path, "strat_close_ok", kind="strategy",
                      plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1h\n"
                                 "pairs: [USDJPY, EURUSD]\nexit_mode: levels\n"
                                 "max_bars: 200\n")
    meta = _strategy_meta(d, name="strat_close_ok", pairs=("USDJPY", "EURUSD"))
    two_pair_settings = settings.model_copy(update={
        "pairs": ["USDJPY", "EURUSD"],
        "backtest": settings.backtest.model_copy(update={"eval_source": "mt5"}),
    })
    conn = _conn(tmp_path)

    created: list[_FakeIntentSource] = []
    seen_sources: list[str] = []

    def fake_build_intent_source(meta_arg, *, conn, pair, source, settings):
        seen_sources.append(source)
        src = _FakeIntentSource(pair)
        created.append(src)
        return src

    def fake_run_in_sample(settings_arg, **kwargs):
        return {"trades": 0}

    with patch("agentic_fx.plugin.approval.strategy_adapter.build_intent_source",
               side_effect=fake_build_intent_source):
        approval.submit_plugin(conn, meta, settings=two_pair_settings, now=NOW,
                               pytest_runner=_ok_pytest_runner,
                               run_in_sample_fn=fake_run_in_sample)

    assert len(created) == 2
    assert all(src.closed for src in created)
    assert seen_sources == ["mt5", "mt5"]


def test_strategy_close_called_even_when_run_in_sample_raises(tmp_path, settings):
    """F3 (レビュー fix round 1, sonnet 変異生存): 既存の fake
    run_in_sample_fn はどれも intent_source を一度も呼ばなかったため、
    session が lazy 未生成のままで `close()` を finally から外す変異が
    19 テスト全部 green のまま生存した (実測)。close() 呼び出しを観測可能
    な fake アダプタを直接注入し、run_in_sample_fn が例外を投げても
    (a) その pair の close() が確実に呼ばれる (b) 以降の pair へは進まない
    ことをピンする。"""
    d = _write_plugin(tmp_path, "strat_close_err", kind="strategy",
                      plugin_py=STRATEGY_PY,
                      config_yaml="kind: strategy\ntimeframe: 1h\n"
                                 "pairs: [USDJPY, EURUSD]\nexit_mode: levels\n"
                                 "max_bars: 200\n")
    meta = _strategy_meta(d, name="strat_close_err", pairs=("USDJPY", "EURUSD"))
    two_pair_settings = settings.model_copy(update={"pairs": ["USDJPY", "EURUSD"]})
    conn = _conn(tmp_path)

    created: list[_FakeIntentSource] = []

    def fake_build_intent_source(meta_arg, *, conn, pair, source, settings):
        src = _FakeIntentSource(pair)
        created.append(src)
        return src

    run_in_sample_calls: list[str] = []

    def crashing_run_in_sample(settings_arg, **kwargs):
        run_in_sample_calls.append(kwargs["symbol"])
        raise RuntimeError("boom")

    with patch("agentic_fx.plugin.approval.strategy_adapter.build_intent_source",
               side_effect=fake_build_intent_source):
        with pytest.raises(RuntimeError, match="boom"):
            approval.submit_plugin(conn, meta, settings=two_pair_settings, now=NOW,
                                   pytest_runner=_ok_pytest_runner,
                                   run_in_sample_fn=crashing_run_in_sample)

    assert run_in_sample_calls == ["USDJPY"]  # 2 pair 目には進まない
    assert len(created) == 1
    assert created[0].closed is True  # 例外が飛んでも finally で close() 済み
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


# --- ⑥ bless (旧 API) は裁定3で常に拒否・何も作らない -------------------
# プラン10 Task11g 裁定3: live path (plugins/<name>) を候補に取る旧
# `approval.bless()` は廃止され常に拒否する (materialize + `bless --from
# _human` を案内するエラー)。以下 2 本は旧テスト
# `test_bless_success_creates_approved_row`/`test_bless_validation_failure_
# creates_no_row` を置換 (逐語の11箇所リストに無い既存テスト破壊 — 裁定3の
# 直接の帰結、最終報告の「逸脱」に明記)。


def test_bless_legacy_api_always_rejected_creates_no_row(tmp_path, settings):
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)

    with pytest.raises(ValueError, match="materialize"):
        approval.bless(conn, meta, settings=settings, now=NOW,
                       pytest_runner=_ok_pytest_runner)
    assert _count_rows(conn) == 0
    assert approvals_store.pending(conn) == []


def test_bless_legacy_api_rejected_even_when_gate_would_have_failed(tmp_path, settings):
    """検証が失敗するはずの入力でも、bless(旧API) は検証に到達する前に
    拒否する (裁定3のエラーが検証結果より優先する)。"""
    d = _write_plugin(tmp_path, "ind", kind="indicator", plugin_py=INDICATOR_PY,
                      config_yaml="kind: indicator\n")
    meta = _indicator_meta(d)
    conn = _conn(tmp_path)

    with pytest.raises(ValueError, match="materialize"):
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


def test_pytest_sandbox_entry_calls_poison_before_pytest():
    """pytest_sandbox_entry.main() が _poison_network_modules を
    pytest.main() より前に呼ぶことを検証 (IMPORTANT)。
    毒入れ削除・順序入替の変異は本テストで赤になることを確認。"""
    from agentic_fx.plugin import pytest_sandbox_entry as entry
    import sys

    call_order = []

    def fake_poison():
        call_order.append('poison')

    def fake_main(args):
        call_order.append('pytest')
        return 0

    with patch.object(sys, 'argv', ['entry', '/tmp/test_plugin.py']), \
         patch('pytest.main', fake_main), \
         patch('agentic_fx.plugin.worker._poison_network_modules', fake_poison):
        try:
            entry.main()
        except SystemExit:
            pass

    assert call_order == ['poison', 'pytest'], \
        f"Expected ['poison', 'pytest'], got {call_order}"
    assert len(call_order) == 2, \
        "Both poison and pytest must be called exactly once each"


# --- 統合テスト①: 既定 pytest_runner が run_gate_pytest に置き替わったこと ---

def test_integration_submit_plugin_uses_gate_pytest_by_default(tmp_path, settings):
    """既定 pytest_runner が `run_gate_pytest`(Landlock 実プロセス)に
    差し替わっていること — pytest_runner を渡さず submit_plugin を呼ぶ。"""
    from agentic_fx.core.landlock import is_available
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")
    d = _write_plugin(tmp_path, "ind_real", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_real")
    conn = _conn(tmp_path)

    started = time.perf_counter()
    approval_id = approval.submit_plugin(conn, meta, settings=settings, now=NOW)
    elapsed = time.perf_counter() - started
    print(f"\n[Task 6 実測] indicator submit (gate pytest, Landlock 実プロセス): "
         f"{elapsed:.3f}s")

    row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    assert row["status"] == "pending"


def test_default_pytest_runner_is_removed():
    """裁定2: `_default_pytest_runner` は削除され参照ゼロ (grep pin)。"""
    from agentic_fx.plugin import approval
    assert not hasattr(approval, "_default_pytest_runner")


def test_pytest_runner_receives_plugin_directory_not_file_path(tmp_path, settings):
    """`pytest_runner` は plugin ディレクトリを受け取る (6-C 契約変更)。
    ファイルパスを渡していた旧実装に戻す変異を kill するテスト。"""
    d = _write_plugin(tmp_path, "ind_dir_check", kind="indicator",
                      plugin_py=INDICATOR_PY, config_yaml="kind: indicator\n")
    meta = _indicator_meta(d, name="ind_dir_check")
    conn = _conn(tmp_path)

    received_path = None

    def spy_runner(path):
        nonlocal received_path
        received_path = path
        return _ok_pytest_runner(path)

    approval_id = approval.submit_plugin(
        conn, meta, settings=settings, now=NOW, pytest_runner=spy_runner)

    # pytest_runner は meta.path (ディレクトリ) を受け取るべき、
    # test_plugin.py (ファイル) ではない
    assert received_path == meta.path
    assert received_path.is_dir()
    assert (received_path / "test_plugin.py").is_file()


# --- 統合テスト②: strategy 経路の実 PluginSession + 実 run_in_sample -----

def _seed_flat(conn, start: datetime, minutes: int, *, price: float,
               source: str = "dukascopy") -> None:
    rows = [("USDJPY", "1m", (start + timedelta(minutes=i)).isoformat(),
             price, price, price, price, 10.0, 0.01) for i in range(minutes)]
    ohlcv_store.import_history_bars(conn, rows, source=source)


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


def test_entry_plugin_bless_without_from_is_always_rejected(
        tmp_path, monkeypatch, capsys):
    """プラン10 Task11 裁定3是正: `afx plugin bless <name>` (--from なし) は
    もはや `approval.bless` へ dispatch しない — 常に拒否し
    'afx plugin materialize' を案内する (旧テスト
    `test_entry_plugin_bless_dispatches_to_approval_bless` を置換。11e Step5
    の CLI 配線変更に伴う既存テスト書き換え — 逐語の 11 箇所リストには
    無いが、裁定3 の直接の帰結として最終報告の「逸脱」に明記する)。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    _write_cli_indicator_plugin(tmp_path / "plugins", "ind")

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service") as run_service, \
         patch("agentic_fx.plugin.approval.bless") as bless_mock:
        rc = main(["plugin", "bless", "ind"])

    assert rc != 0
    run_service.assert_not_called()
    assert not bless_mock.called
    # 検収 m9 是正: 他の全 CLI エラーと同じく stderr へ統一 (旧稿は stdout)
    assert "materialize" in capsys.readouterr().err


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

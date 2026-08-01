"""tests/backtest/test_cli.py — 人間 CLI (history / backtest / analyze) の
配線テスト (プラン 6 Task 11)。

importer/runner/metrics/analysis 自体は Task 4-10 で既にテスト済みなので、
ここでは CLI (argparse 配線・DB 解決・scope 記録) だけを検証する。
``tests/test_entry.py`` は実リポジトリに存在しないため、entry.py 側の新規
テストもここに置く (task-11-brief.md 上書き節 A)。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import agentic_fx.backtest.cli as cli
from agentic_fx.backtest.runner import BacktestResult
from agentic_fx.entry import main
from agentic_fx.store.db import connect

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_settings(root: Path) -> None:
    """load_settings が読める config/settings.yaml を tmp root に置く。"""
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO_ROOT / "config" / "settings.yaml.example",
               root / "config" / "settings.yaml")


# ---- history import ---------------------------------------------------


def test_cli_history_import_calls_importer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.import_dukascopy") as imp:
        imp.return_value = MagicMock(inserted=10, unchanged=0, conflicted=0)
        rc = main(["history", "import", "--source", "dukascopy",
                   "--symbol", "USDJPY",
                   "--from", "2026-07-01", "--to", "2026-07-02"])
    assert rc == 0
    args, kwargs = imp.call_args
    assert args[1] == "USDJPY"  # (conn, symbol, start, end)
    assert args[2].isoformat().startswith("2026-07-01")
    assert args[2].tzinfo is timezone.utc


def test_cli_history_import_mt5_dispatches_to_import_mt5(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.import_dukascopy") as duka, \
         patch("agentic_fx.backtest.cli.import_mt5") as mt5imp:
        mt5imp.return_value = MagicMock(inserted=1, unchanged=0, conflicted=0)
        # settings.yaml.example の datafeed.mt5.bridge_url は設定済み
        rc = main(["history", "import", "--source", "mt5",
                   "--symbol", "USDJPY",
                   "--from", "2026-07-01", "--to", "2026-07-02"])
    assert rc == 0
    duka.assert_not_called()
    assert mt5imp.called
    _, kwargs = mt5imp.call_args
    assert kwargs["base_url"] == "http://localhost:8812"


def test_cli_history_import_mt5_requires_bridge_url(tmp_path, monkeypatch):
    """bridge_url 未設定は fail closed (rc=1) — 上書き節 D。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    # settings.yaml の mt5.bridge_url を None にする
    text = (tmp_path / "config" / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace(
        'mt5:        {enabled: false, bridge_url: "http://localhost:8812"}',
        'mt5:        {enabled: false, bridge_url: null}')
    (tmp_path / "config" / "settings.yaml").write_text(text, encoding="utf-8")
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.import_mt5") as mt5imp:
        rc = main(["history", "import", "--source", "mt5",
                   "--symbol", "USDJPY",
                   "--from", "2026-07-01", "--to", "2026-07-02"])
    assert rc == 1
    mt5imp.assert_not_called()


# ---- history compare / coverage ----------------------------------------


def test_cli_history_compare_prints_result(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.compare_sources") as cmp:
        cmp.return_value = {"count": 0, "mean": None, "std": None,
                            "max_abs": None}
        rc = main(["history", "compare", "--symbol", "USDJPY"])
    assert rc == 0
    assert cmp.called
    out = capsys.readouterr().out
    assert "None" in out


def test_cli_history_coverage_calls_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.coverage_report") as cov:
        cov.return_value = {"bars": 1, "expected_open_bars": 1,
                            "gap_pct": 0.0}
        rc = main(["history", "coverage", "--symbol", "EURUSD",
                   "--timeframe", "1h", "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02"])
    assert rc == 0
    _, kwargs = cov.call_args
    assert kwargs["timeframe"] == "1h" and kwargs["source"] == "dukascopy"
    assert kwargs["start"].isoformat().startswith("2026-07-01")
    assert kwargs["end"].isoformat().startswith("2026-07-02")


# ---- backtest run (mock 版 — brief 上書き節 F の読み替え) -----------------


def test_cli_backtest_run_records_human_custom_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text("", encoding="utf-8")  # 提案なし = 取引 0 で完走
    fake_result = BacktestResult(
        orders=[], equity_curve=[("2026-07-01T00:00:00+00:00", 1_000_000.0)],
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 2, tzinfo=timezone.utc),
        source="dukascopy", fallback_spread_used=False)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rr.return_value = fake_result
        br.save_human_run.return_value = 1
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(proposals)])
    assert rc == 0
    assert br.save_human_run.called  # scope/issued_by は save_human_run 内部で固定
    _, kwargs = br.save_human_run.call_args
    assert kwargs["pair"] == "USDJPY"
    assert kwargs["period"] == (
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert kwargs["kind"] == "proposals"
    assert kwargs["timeframe"] == "1h"


def test_cli_backtest_run_rejects_naive_ts_in_proposal_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text(
        '{"ts": "2026-07-01T00:00:00", "action": "hold", "reasoning": "x"}\n',
        encoding="utf-8")
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(proposals)])
    assert rc == 1
    rr.assert_not_called()


def test_cli_backtest_run_rejects_unparsable_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text("not json\n", encoding="utf-8")
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(proposals)])
    assert rc == 1
    rr.assert_not_called()


def test_proposal_intent_source_consumes_oldest_first_one_per_bar():
    from agentic_fx.core.contracts import Bar

    t0 = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)
    proposals = [(t0, {"a": 1}), (t1, {"a": 2})]
    src = cli._ProposalIntentSource(proposals)

    bar_before = Bar(symbol="USDJPY", interval="1h", ts=t0 - timedelta(minutes=1),
                    open=1, high=1, low=1, close=1, volume=1)
    assert src(bar_before) is None  # まだ最古提案の ts に届いていない

    bar_at_t0 = Bar(symbol="USDJPY", interval="1h", ts=t0, open=1, high=1,
                    low=1, close=1, volume=1)
    assert src(bar_at_t0) == {"a": 1}  # 1 件消費
    assert src(bar_at_t0) is None      # 同バーでもう 1 件は出さない (次の提案は未到達)

    bar_at_t1 = Bar(symbol="USDJPY", interval="1h", ts=t1, open=1, high=1,
                    low=1, close=1, volume=1)
    assert src(bar_at_t1) == {"a": 2}


# ---- backtest run (統合版 — 実 DB・実 run_replay。上書き節 F 逐語) ---------


def test_cli_backtest_run_integration_writes_human_custom_row(tmp_path,
                                                               monkeypatch):
    """一時 root + 実 DB ファイル + 実 run_replay で human_custom 行を確認する。

    期間は 2 時間 (~3.2ms/tick 実測、Task 9 照合済み)。履歴なしで取引 0 の
    完走でよい (上書き節 F)。市場オープン時間帯 (conftest の H = 水曜 12:00
    UTC) を使う。core_commit は実 git を避けて patch する。

    提案ファイルは空にしない (2099 年の ts = 再生期間中は絶対消費されない)
    — content_hash/plugin_ref が空バイト列のハッシュ (定数)
    に固定化されてしまうと、定数化変異が偶然一致して SURVIVED になり得る。
    """
    from tests.backtest.conftest import H

    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    from agentic_fx.store.state import StateStore
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(
        initialized=True)

    proposals = tmp_path / "p.jsonl"
    proposals.write_text(
        '{"ts": "2099-01-01T00:00:00+00:00", "action": "hold", '
        '"reasoning": "never reached in this window"}\n',
        encoding="utf-8")

    start = H
    end = H + timedelta(hours=2)

    with patch("agentic_fx.store.backtest_runs.core_commit",
               return_value="deadbeef"):
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", start.isoformat(), "--to", end.isoformat(),
                   "--proposal-file", str(proposals)])
    assert rc == 0

    conn = connect(tmp_path / "data" / "agentic.db")
    rows = conn.execute("SELECT * FROM backtest_runs").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["scope"] == "human_custom"
    assert row["issued_by"] == "human_cli"
    assert row["period_start"] == start.isoformat()
    assert row["period_end"] == end.isoformat()
    assert row["pair"] == "USDJPY"
    assert row["core_commit"] == "deadbeef"
    import hashlib
    assert row["content_hash"] == hashlib.sha256(
        proposals.read_bytes()).hexdigest()
    assert row["plugin_ref"] == str(proposals.resolve())


def test_load_proposals_sorts_ascending_and_strips_ts(tmp_path):
    """``_load_proposals`` の実行内容 (JSONL parse・ts 昇順整列・ts キー除去)
    を直接検証する — CLI 経由のテストは中身に触れないため。"""
    path = tmp_path / "p.jsonl"
    path.write_text(
        '{"ts": "2026-07-22T13:00:00+00:00", "action": "hold", "reasoning": "second"}\n'
        '\n'  # 空行は無視される
        '{"ts": "2026-07-22T12:00:00+00:00", "action": "hold", "reasoning": "first"}\n',
        encoding="utf-8")
    proposals = cli._load_proposals(path)
    assert [ts for ts, _ in proposals] == [
        datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)]
    assert [intent["reasoning"] for _, intent in proposals] == ["first", "second"]
    for _, intent in proposals:
        assert "ts" not in intent


# ---- analyze corr --------------------------------------------------------


def test_cli_analyze_corr_prints_pair_value_and_does_not_save(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.corr_matrix") as cm:
        cm.return_value = {("USDJPY", "EURUSD"): 0.42}
        rc = main(["analyze", "corr", "--a", "USDJPY", "--b", "EURUSD",
                   "--timeframe", "1h", "--source", "dukascopy"])
    assert rc == 0
    _, kwargs = cm.call_args
    assert kwargs["in_sample_until"] == cli._NO_LIMIT  # --to 未指定 → far future
    assert kwargs["since"] is None                     # --from 未指定
    out = capsys.readouterr().out
    assert "0.42" in out


def test_cli_analyze_corr_passes_from_as_since_and_to_as_in_sample_until(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.corr_matrix") as cm:
        cm.return_value = {("USDJPY", "EURUSD"): 0.1}
        rc = main(["analyze", "corr", "--a", "USDJPY", "--b", "EURUSD",
                   "--timeframe", "1h", "--source", "dukascopy",
                   "--from", "2026-01-01", "--to", "2026-06-01"])
    assert rc == 0
    _, kwargs = cm.call_args
    assert kwargs["since"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert kwargs["in_sample_until"] == datetime(2026, 6, 1,
                                                 tzinfo=timezone.utc)


# ---- 既定サービス動作の不変性 (上書き節 A) --------------------------------


def test_cli_default_service_behavior_unchanged(monkeypatch):
    with patch("agentic_fx.service.run_service", return_value=0) as rs:
        rc = main([])
    assert rc == 0 and rs.called

"""tests/backtest/test_cli.py — 人間 CLI (history / backtest / analyze) の
配線テスト (プラン 6 Task 11)。

importer/runner/metrics/analysis 自体は Task 4-10 で既にテスト済みなので、
ここでは CLI (argparse 配線・DB 解決・scope 記録) だけを検証する。
``tests/test_entry.py`` は実リポジトリに存在しないため、entry.py 側の新規
テストもここに置く (task-11-brief.md 上書き節 A)。
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agentic_fx.backtest.cli as cli
from agentic_fx.backtest import runner as runner_module
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.runner import BacktestResult
from tests.backtest.factories import DATASET_1M
from agentic_fx.entry import main
from agentic_fx.store import backtest_runs as backtest_runs_real
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_settings(root: Path) -> None:
    """load_settings が読める config/settings.yaml を tmp root に置く。"""
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO_ROOT / "config" / "settings.yaml.example",
               root / "config" / "settings.yaml")


_OPEN_MARKET_LINE = (
    '{{"ts": "{ts}", "action": "open", "pair": "USDJPY", "direction": "long", '
    '"entry_type": "market", "horizon": "day", "stop_loss": 148.30, '
    '"take_profit": 149.00, "reasoning": "{reasoning}"}}\n')


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
    assert kwargs["timeframe"] == "1h"
    assert kwargs["dataset"].source == "dukascopy"
    assert kwargs["dataset"].base_interval == "1m"
    assert kwargs["start"].isoformat().startswith("2026-07-01")
    assert kwargs["end"].isoformat().startswith("2026-07-02")


# ---- backtest run (mock 版 — brief 上書き節 F の読み替え) -----------------


def test_cli_backtest_run_records_human_custom_scope(tmp_path, monkeypatch):
    """Fix Round 1 F2 (sonnet I-2 — 自己変異 M-B SURVIVED の是正):
    `source`/`settings_hash`/`initial_balance` を直接検証する。
    `settings_hash` は実 `settings_snapshot_hash` に委譲する
    (`backtest_runs` を丸ごと mock しても、実装が使う計算式そのものと
    突き合わせるため — 単なる値の素通りでは検出できない)。
    """
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text("", encoding="utf-8")  # 提案なし = 取引 0 で完走
    # F4 (最終レビュー opus I-4) の空履歴 fail closed ガードを通すため、
    # 対象 source/期間に 1m 履歴を最低 1 行 seed する (このテストの主眼は
    # run_replay を mock した配線検証であり、ガード自体は
    # test_cli_backtest_run_rejects_empty_history が別途検証する)。
    from agentic_fx.store import ohlcv as ohlcv_store
    seed_conn = connect(tmp_path / "data" / "agentic.db")
    init_db(seed_conn)
    ohlcv_store.import_history_bars(
        seed_conn, [("USDJPY", "1m", "2026-07-01T00:00:00+00:00",
                    148.0, 148.2, 147.9, 148.1, 10.0, 0.01)],
        source="dukascopy")
    seed_conn.close()
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
        br.settings_snapshot_hash.side_effect = \
            backtest_runs_real.settings_snapshot_hash
        br.core_commit.return_value = "deadbeef"
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
    assert kwargs["source"] == "dukascopy"
    from agentic_fx.config import load_settings
    settings = load_settings(tmp_path / "config" / "settings.yaml")
    assert kwargs["settings_hash"] == \
        backtest_runs_real.settings_snapshot_hash(settings)
    assert kwargs["initial_balance"] == settings.backtest.initial_balance


def test_cli_backtest_run_rejects_empty_history(tmp_path, monkeypatch):
    """F4 (最終レビュー opus I-4): 対象 source/期間に 1m 履歴が 0 行なら
    run_replay を呼ばず rc=1 (--source のタイプミス等が正常系と見分けの
    つかない human_custom 行を残さないこと)。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text("", encoding="utf-8")  # 履歴は投入しない (DB は空)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(proposals)])
    assert rc == 1
    rr.assert_not_called()
    br.save_human_run.assert_not_called()


def test_cli_backtest_run_empty_history_guard_uses_base_interval_arg(
        tmp_path, monkeypatch):
    """段 0 pin: `_empty_history_guard` の COUNT クエリが literal "1m" に
    固定戻しすると、5m 基底で実データが在るのに rc=1 (fail closed) の
    誤検出になる — ``args.base_interval`` を使うことを pin する。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text("", encoding="utf-8")
    from agentic_fx.store import ohlcv as ohlcv_store
    seed_conn = connect(tmp_path / "data" / "agentic.db")
    init_db(seed_conn)
    ohlcv_store.import_history_bars(
        seed_conn, [("USDJPY", "5m", "2026-07-01T00:00:00+00:00",
                    148.0, 148.2, 147.9, 148.1, 10.0, 0.01)],
        source="mt5")
    seed_conn.close()
    fake_result = BacktestResult(
        orders=[], equity_curve=[("2026-07-01T00:00:00+00:00", 1_000_000.0)],
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 2, tzinfo=timezone.utc),
        source="mt5", fallback_spread_used=False)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rr.return_value = fake_result
        br.save_human_run.return_value = 1
        br.settings_snapshot_hash.side_effect = \
            backtest_runs_real.settings_snapshot_hash
        br.core_commit.return_value = "deadbeef"
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "mt5", "--base-interval", "5m",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(proposals)])
    assert rc == 0
    rr.assert_called_once()


def test_cli_backtest_run_rejects_naive_ts_in_proposal_file(tmp_path, monkeypatch):
    """ts の naive 性だけを不正要因として孤立させる (Fix Round 1 レビュー
    指摘: action='open' の完全な有効 intent に naive ts だけを混ぜることで、
    action 非 open 由来ではなく naive ts 由来で rc=1 になることをピンする —
    でないと F3 の action 制約がこのテストの合否理由を静かに乗っ取る)。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text(
        _OPEN_MARKET_LINE.format(ts="2026-07-01T00:00:00", reasoning="x"),
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


# ---- backtest run --plugin (プラン 7 Task 5 — --proposal-file と相互排他) ---


def _write_strategy_plugin(plugins_dir: Path, name: str, *, pairs: list[str],
                           timeframe: str = "1h",
                           plugin_body: str | None = None) -> Path:
    """discover/check_source を通る最小限の strategy plugin フォルダを作る。"""
    d = plugins_dir / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_body or (
        "def evaluate(df, indicators, signals, params):\n"
        '    return {"action": "hold", "rationale": "noop"}\n'))
    pairs_yaml = "[" + ", ".join(pairs) + "]"
    (d / "config.yaml").write_text(
        f"kind: strategy\ntimeframe: {timeframe}\npairs: {pairs_yaml}\n"
        "exit_mode: levels\nmax_bars: 200\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return d


def test_cli_backtest_run_requires_plugin_xor_proposal_file():
    """opus R2 M6: --proposal-file の required=True を解除し、--plugin との
    相互排他 (どちらか一方必須) にする — どちらも指定しないと argparse が
    SystemExit(2) で弾く (dispatch にすら到達しない)。"""
    with pytest.raises(SystemExit) as exc_info:
        main(["backtest", "run", "--symbol", "USDJPY", "--source", "dukascopy",
             "--from", "2026-07-01", "--to", "2026-07-02"])
    assert exc_info.value.code == 2


def test_cli_backtest_run_rejects_both_plugin_and_proposal_file():
    with pytest.raises(SystemExit) as exc_info:
        main(["backtest", "run", "--symbol", "USDJPY", "--source", "dukascopy",
             "--from", "2026-07-01", "--to", "2026-07-02",
             "--proposal-file", "p.jsonl", "--plugin", "strat"])
    assert exc_info.value.code == 2


def test_cli_backtest_run_plugin_records_strategy_scope(tmp_path, monkeypatch,
                                                          capsys):
    """`--plugin` 経路は discover → check_source (承認は要求しない) →
    pairs 照合 → `save_human_run(plugin_ref=..., content_hash=meta.content_hash,
    kind="strategy")`。run_replay を mock しているので intent_source は
    一度も発火せず (eval_count=0)、stderr に警告が出ることも併せて確認する。

    F2 (sonnet Important — レビュー fix round 1): 以前は `save_human_run`
    の kwargs しか見ておらず、`build_intent_source(pair=args.symbol)` を
    固定値へすり替える変異・`run_replay(eval_timeframe=args.timeframe)` を
    固定値へすり替える変異のいずれも 28 件 green のまま生存した (レビュ
    アー実測)。`strategy_adapter.build_intent_source` を patch して呼び
    出し kwargs (pair/source) を直接検証し、`run_replay` に渡った
    symbol/source/eval_timeframe と intent_source の同一性 (patch の
    戻り値そのものが渡っていること) も検証する。`--timeframe 2h` を明示
    指定する (既定 "1h" とも、レビュアーが実測した具体的な固定値 "4h" と
    も異なる値にすることで、どちらの偶然一致にも頼らない)。
    """
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    from agentic_fx.plugin.loader import content_hash as real_content_hash
    from agentic_fx.store import ohlcv as ohlcv_store
    plugin_dir = _write_strategy_plugin(tmp_path / "plugins", "strat",
                                        pairs=["USDJPY"])
    seed_conn = connect(tmp_path / "data" / "agentic.db")
    init_db(seed_conn)
    ohlcv_store.import_history_bars(
        seed_conn, [("USDJPY", "1m", "2026-07-01T00:00:00+00:00",
                    148.0, 148.2, 147.9, 148.1, 10.0, 0.01)],
        source="dukascopy")
    seed_conn.close()
    fake_result = BacktestResult(
        orders=[], equity_curve=[("2026-07-01T00:00:00+00:00", 1_000_000.0)],
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 2, tzinfo=timezone.utc),
        source="dukascopy", fallback_spread_used=False)
    sentinel_source = MagicMock()
    sentinel_source.eval_count = 0
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br, \
         patch("agentic_fx.backtest.cli.strategy_adapter.build_intent_source",
               return_value=sentinel_source) as build_src:
        rr.return_value = fake_result
        br.save_human_run.return_value = 1
        br.settings_snapshot_hash.side_effect = \
            backtest_runs_real.settings_snapshot_hash
        br.core_commit.return_value = "deadbeef"
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--timeframe", "2h",
                   "--plugin", "strat"])
    assert rc == 0

    assert build_src.called
    _, build_kwargs = build_src.call_args
    assert build_kwargs["pair"] == "USDJPY"
    assert build_kwargs["dataset"].source == "dukascopy"

    assert rr.called
    _, rr_kwargs = rr.call_args
    assert rr_kwargs["symbol"] == "USDJPY"
    assert rr_kwargs["dataset"].source == "dukascopy"
    assert rr_kwargs["eval_timeframe"] == "2h"
    assert rr_kwargs["intent_source"] is sentinel_source  # build_src の戻り値そのもの

    assert br.save_human_run.called
    _, kwargs = br.save_human_run.call_args
    assert kwargs["plugin_ref"] == "plugins/strat"
    assert kwargs["content_hash"] == real_content_hash(plugin_dir)
    assert kwargs["kind"] == "strategy"
    assert kwargs["pair"] == "USDJPY"
    assert kwargs["timeframe"] == "2h"
    assert kwargs["source"] == "dukascopy"
    err = capsys.readouterr().err
    assert "警告" in err  # eval_count=0 (sentinel_source) の観測性警告
    sentinel_source.close.assert_called_once()  # try/finally が close() を呼ぶ


def test_cli_backtest_run_plugin_closes_session_even_if_run_replay_raises(
        tmp_path, monkeypatch, capsys):
    """F3 (sonnet Important — レビュー fix round 1): `try/finally:
    intent_source.close()` を丸ごと削っても既存テストは green のまま
    だった (レビュアー実測 — 既存テストは発火 0 回で close() が no-op の
    ため検出できなかった)。`run_replay` に例外 (`SandboxError` —
    strategy_adapter の fail closed 契約どおり評価時に伝播し得る) を投げ
    させ、close() が呼ばれることを確認する。

    F2 (最終レビュー — 両レビュー一致): 以前は SandboxError が dispatch
    まで素通しされ、人間向け CLI が生 traceback で落ちていた (「人間向け
    CLI は生 traceback を出さない」の唯一の例外)。ここでは CLI 境界
    (`_backtest_run_plugin`) で SandboxError を catch し、①`main()` が
    例外を送出せず rc=1 を返すこと (= 生 traceback が出ないこと)
    ②stderr に診断メッセージが出ること ③`backtest_runs.save_human_run`
    が呼ばれないこと (fail closed — human_custom 行を残さない) の 3 点を
    確認する。strategy_adapter 層の「SandboxError を hold へ読み替えず
    run_replay を止める」契約自体は変えていない (`run_replay` は依然
    SandboxError で例外終了する) — 変換するのはこの CLI 境界だけ。"""
    from agentic_fx.plugin.sandbox import SandboxError

    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    from agentic_fx.store import ohlcv as ohlcv_store
    _write_strategy_plugin(tmp_path / "plugins", "strat", pairs=["USDJPY"])
    seed_conn = connect(tmp_path / "data" / "agentic.db")
    init_db(seed_conn)
    ohlcv_store.import_history_bars(
        seed_conn, [("USDJPY", "1m", "2026-07-01T00:00:00+00:00",
                    148.0, 148.2, 147.9, 148.1, 10.0, 0.01)],
        source="dukascopy")
    seed_conn.close()

    sentinel_source = MagicMock()
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay",
               side_effect=SandboxError("plugin crashed mid-replay")) as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br, \
         patch("agentic_fx.backtest.cli.strategy_adapter.build_intent_source",
               return_value=sentinel_source):
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                  "--source", "dukascopy",
                  "--from", "2026-07-01", "--to", "2026-07-02",
                  "--plugin", "strat"])
    assert rc == 1
    assert rr.called
    sentinel_source.close.assert_called_once()
    br.save_human_run.assert_not_called()  # run_replay が例外なので保存まで進まない
    err = capsys.readouterr().err
    assert "plugin crashed mid-replay" in err
    assert "Traceback" not in err


def test_cli_backtest_run_plugin_not_found_rc1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    (tmp_path / "plugins").mkdir()
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--plugin", "does-not-exist"])
    assert rc == 1
    rr.assert_not_called()
    br.save_human_run.assert_not_called()


def test_cli_backtest_run_plugin_missing_plugins_dir_rc1(tmp_path, monkeypatch):
    """plugins/ フォルダ自体が無い場合も discover の FileNotFoundError で
    クラッシュせず rc=1 (loader.discover は存在確認を呼び出し元に要求する
    契約 — CLI 側で先に .is_dir() を見る)。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--plugin", "strat"])
    assert rc == 1
    rr.assert_not_called()
    br.save_human_run.assert_not_called()


def test_cli_backtest_run_plugin_symbol_not_in_pairs_rc1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    _write_strategy_plugin(tmp_path / "plugins", "strat", pairs=["EURUSD"])
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--plugin", "strat"])
    assert rc == 1
    rr.assert_not_called()
    br.save_human_run.assert_not_called()


def test_cli_backtest_run_plugin_check_source_rejects_rc1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    _write_strategy_plugin(
        tmp_path / "plugins", "strat", pairs=["USDJPY"],
        plugin_body=(
            "import os\n"
            "def evaluate(df, indicators, signals, params):\n"
            '    return {"action": "hold", "rationale": "noop"}\n'))
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--plugin", "strat"])
    assert rc == 1
    rr.assert_not_called()
    br.save_human_run.assert_not_called()


def test_cli_backtest_run_plugin_kind_mismatch_rc1(tmp_path, monkeypatch):
    """kind=indicator の plugin を --plugin に指定した場合も rc=1 (strategy
    以外の kind は strategy_adapter の契約と合わない — fail closed)。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    d = tmp_path / "plugins" / "ind"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (d / "config.yaml").write_text("kind: indicator\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--plugin", "ind"])
    assert rc == 1
    rr.assert_not_called()
    br.save_human_run.assert_not_called()


# ---- backtest run (統合版 — 実 DB・実 run_replay。上書き節 F 逐語) ---------


def test_cli_backtest_run_integration_writes_human_custom_row(tmp_path,
                                                               monkeypatch):
    """一時 root + 実 DB ファイル + 実 run_replay で human_custom 行を確認する。

    期間は 2 時間 (~3.2ms/tick 実測、Task 9 照合済み)。市場オープン時間帯
    (factories の H = 水曜 12:00 UTC) を使う。core_commit は実 git を避けて
    patch する。

    Fix Round 1 F1 (sonnet I-1 — 自己変異 M-A SURVIVED の是正): 従来はこの
    テストが履歴なし・2099 年 ts (再生期間中は絶対消費されない) だったため、
    `intent_source=intent_source` を `intent_source=lambda _bar: None` に
    差し替える変異が全テスト green のまま SURVIVED していた。これは
    「人間が用意した提案が実際に run_replay 経由で executor まで届くこと」
    という Task 11 の中核機能を証明していなかった。ここでは (1) 再生期間の
    先頭 1 時間強を覆う 1m ohlcv を実 DB に事前 seed し、(2) 期間内の ts
    (=H) を持つ有効な open (market) intent を proposal-file に置き、
    (3) `run_replay` を実体呼び出しに委譲する spy で結果を捕捉して
    `orders` テーブル相当の行が実際に生成されたことを assert する —
    `metrics.trades` は closed のみを数えるため、2 時間程度の短い窓では
    ポジションが閉じない可能性が高く指標として不適切 (レビュー指摘どおり、
    `result.orders` の件数を直接見る)。
    """
    from tests.backtest.factories import H

    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(
        initialized=True)

    # (1) 再生期間の先頭を覆う 1m ohlcv を実 DB に事前 seed する。
    #     12:00-13:00 (61 本、フラット) — 13:01 tick での成行執行が使う
    #     quote (13:00 の完成バー) までを覆う。test_runner.py の
    #     `test_pending_execution_happens_after_tick_not_before` と同型。
    hist_conn = connect(tmp_path / "data" / "agentic.db")
    init_db(hist_conn)
    from agentic_fx.store import ohlcv as ohlcv_store
    rows = [
        (
            "USDJPY", "1m", (H + timedelta(minutes=i)).isoformat(),
            148.5, 148.6, 148.4, 148.5, 10.0, 0.01,
        )
        for i in range(61)  # H (12:00) から 13:00 まで inclusive
    ]
    ohlcv_store.import_history_bars(hist_conn, rows, source="dukascopy")
    hist_conn.close()

    # (2) 期間内 (ts=H) の有効な open (market) 提案。フラット相場なので
    #     SL/TP には触れず「open のまま」残る (closed にはならない —
    #     metrics.trades では検出できないことの実演でもある)。
    proposals = tmp_path / "p.jsonl"
    proposals.write_text(
        _OPEN_MARKET_LINE.format(ts=H.isoformat(), reasoning="f1 integration"),
        encoding="utf-8")

    start = H
    end = H + timedelta(hours=2)

    # (3) run_replay を実体へ委譲する spy — 実行経路は変えず、CLI が
    #     受け取るのと同じ BacktestResult を横取りして orders を検証する。
    captured: dict[str, object] = {}

    def _spy_run_replay(*args, **kwargs):
        result = runner_module.run_replay(*args, **kwargs)
        captured["result"] = result
        return result

    with patch("agentic_fx.backtest.cli.run_replay",
               side_effect=_spy_run_replay), \
         patch("agentic_fx.store.backtest_runs.core_commit",
               return_value="deadbeef"):
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", start.isoformat(), "--to", end.isoformat(),
                   "--proposal-file", str(proposals)])
    assert rc == 0

    # 提案が実際に intent_source 経由で消費され、executor まで届いたことの
    # 実証 (intent_source が無効化されると orders は空になり、ここが落ちる)。
    assert len(captured["result"].orders) >= 1
    assert captured["result"].orders[0]["pair"] == "USDJPY"

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
    assert row["content_hash"] == hashlib.sha256(
        proposals.read_bytes()).hexdigest()
    assert row["plugin_ref"] == str(proposals.resolve())


def test_load_proposals_sorts_ascending_and_strips_ts(tmp_path):
    """``_load_proposals`` の実行内容 (JSONL parse・ts 昇順整列・ts キー除去)
    を直接検証する — CLI 経由のテストは中身に触れないため。"""
    path = tmp_path / "p.jsonl"
    path.write_text(
        _OPEN_MARKET_LINE.format(ts="2026-07-22T13:00:00+00:00",
                                 reasoning="second")
        + '\n'  # 空行は無視される
        + _OPEN_MARKET_LINE.format(ts="2026-07-22T12:00:00+00:00",
                                   reasoning="first"),
        encoding="utf-8")
    proposals = cli._load_proposals(path)
    assert [ts for ts, _ in proposals] == [
        datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)]
    assert [intent["reasoning"] for _, intent in proposals] == ["first", "second"]
    for _, intent in proposals:
        assert "ts" not in intent


def test_load_proposals_rejects_non_open_action(tmp_path):
    """Fix Round 1 F3 (裁定: 許容 action は open のみ)。"""
    path = tmp_path / "p.jsonl"
    path.write_text(
        '{"ts": "2026-07-22T12:00:00+00:00", "action": "hold", '
        '"reasoning": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        cli._load_proposals(path)


def test_load_proposals_rejects_malformed_open_intent_before_any_execution(
        tmp_path, monkeypatch):
    """Fix Round 1 F3 (sonnet I-4 = codex Important): intent 本体の形状不正
    (必須キー欠落) は run_replay 実行前に検出される — 前半行が有効でも
    後半行が不正なら何も実行しない (fail closed、部分実行しない)。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    path = tmp_path / "p.jsonl"
    path.write_text(
        _OPEN_MARKET_LINE.format(ts="2026-07-22T12:00:00+00:00",
                                 reasoning="valid line")
        # 2 行目: stop_loss 欠落 (open の必須キー) — TradeIntent.from_llm_dict
        # が IntentParseError (ValueError のサブクラス) を送出するはず
        + '{"ts": "2026-07-22T13:00:00+00:00", "action": "open", '
        '"pair": "USDJPY", "direction": "long", "entry_type": "market", '
        '"horizon": "day", "reasoning": "missing stop_loss"}\n',
        encoding="utf-8")
    with pytest.raises(ValueError):
        cli._load_proposals(path)

    # dispatch 経由でも rc=1・run_replay は一度も呼ばれない (部分実行なし)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.backtest.cli.run_replay") as rr:
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(path)])
    assert rc == 1
    rr.assert_not_called()


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


def test_cli_analyze_corr_does_not_write_to_analysis_runs_real_db(
        tmp_path, monkeypatch, capsys):
    """Fix Round 1 F4 (codex I2): mock `corr_matrix` では「analysis_runs に
    保存しない」契約を実質検証できていなかった。実 DB + 実 `corr_matrix`
    で `analysis_runs` が 0 行のままであることを直接ピンする。"""
    import math

    from agentic_fx.store import ohlcv as ohlcv_store

    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(
        initialized=True)

    conn = connect(tmp_path / "data" / "agentic.db")
    init_db(conn)
    start = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    rows_a, rows_b = [], []
    # プラン 7 Task 0: 分析関数は ohlcv の 1m 行から読み取り時リサンプルする
    # ので (本ブランチのインポータは 1m のみ書く)、ここも interval="1m" で
    # 1h 刻みに 1 本ずつ投入する (resample は「在る分だけ」の集約なので、
    # 旧 interval="1h" 直接投入と同じ close 系列・行数になる)。
    for i in range(45):  # MIN_COMMON_OBS(30) を十分上回る決定的系列
        t = (start + timedelta(hours=i)).isoformat()
        va = 100 + math.sin(i / 5.0)
        vb = 50 + math.sin(i / 5.0 + 0.3)
        rows_a.append(("USDJPY", "1m", t, va, va + 0.05, va - 0.05, va, 1.0,
                       0.01))
        rows_b.append(("EURUSD", "1m", t, vb, vb + 0.05, vb - 0.05, vb, 1.0,
                       0.01))
    ohlcv_store.import_history_bars(conn, rows_a, source="dukascopy")
    ohlcv_store.import_history_bars(conn, rows_b, source="dukascopy")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM analysis_runs").fetchone()["n"] == 0
    conn.close()

    rc = main(["analyze", "corr", "--a", "USDJPY", "--b", "EURUSD",
               "--timeframe", "1h", "--source", "dukascopy"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out  # 相関値が実際に出力されている (mock ではなく実計算)

    conn2 = connect(tmp_path / "data" / "agentic.db")
    assert conn2.execute(
        "SELECT COUNT(*) AS n FROM analysis_runs").fetchone()["n"] == 0


def test_cli_analyze_corr_invalid_timeframe_returns_rc1_without_traceback(
        tmp_path, monkeypatch, capsys):
    """Fix Round 1 F6 (sonnet I-3): 統一エラー境界。列挙外 `--timeframe` は
    `corr_matrix` 内部の `ValueError` (`_validate_timeframe`) を rc=1 に
    変換し、生の traceback を出さない。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"):
        rc = main(["analyze", "corr", "--a", "USDJPY", "--b", "EURUSD",
                   "--timeframe", "3m", "--source", "dukascopy"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err


# ---- init 未完時の実ガード (上書き節 C / Fix Round 1 F5) -------------------


def test_cli_history_coverage_requires_init(tmp_path, monkeypatch):
    """Fix Round 1 F5 (codex I3): 全 CLI テストが `ensure_initialized` を
    patch しているため、dispatch からその呼び出しを削除しても検出できな
    かった。ここでは patch せず、init 未完 root からサブコマンドを 1 つ
    呼んで実ガード (`SystemExit(2)`) を固定する。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    # StateStore を initialized=True にしない — 未初期化のまま
    with pytest.raises(SystemExit) as exc_info:
        main(["history", "coverage", "--symbol", "USDJPY",
              "--timeframe", "1h", "--source", "dukascopy",
              "--from", "2026-07-01", "--to", "2026-07-02"])
    assert exc_info.value.code == 2


# ---- Task 3: sqlite3.Error CLI 境界 -------------------------------------------

def test_dispatch_sqlite_error_returns_rc1_with_diagnostic(tmp_path, capsys, monkeypatch):
    """DB 層の sqlite3.Error が生の traceback ではなく診断メッセージ +
    rc=1 に正規化される (Task 6 deferred④、裁定書 F6)。"""
    import argparse
    import sqlite3

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_history_coverage", boom)
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"):
        args = argparse.Namespace(command="history", history_command="coverage",
                                  symbol="USDJPY", timeframe="1h",
                                  source="dukascopy", base_interval="1m",
                                  **{"from": "2026-07-01", "to": "2026-07-02"})
        rc = cli.dispatch(args, tmp_path)
    assert rc == 1
    assert "database is locked" in capsys.readouterr().err


def test_dispatch_sqlite_error_on_connect_returns_rc1_with_diagnostic(
        tmp_path, capsys, monkeypatch):
    """cli.connect が sqlite3.Error を起こした場合、生の traceback ではなく
    診断メッセージ + rc=1 に正規化される (codex I-2: DB 初期化境界)。"""
    import argparse
    import sqlite3

    def boom(*a, **k):
        raise sqlite3.OperationalError("cannot open database file")

    monkeypatch.setattr(cli, "connect", boom)
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"):
        args = argparse.Namespace(command="history", history_command="coverage",
                                  symbol="USDJPY", timeframe="1h",
                                  source="dukascopy", base_interval="1m",
                                  **{"from": "2026-07-01", "to": "2026-07-02"})
        rc = cli.dispatch(args, tmp_path)
    assert rc == 1
    assert "cannot open database file" in capsys.readouterr().err


def test_dispatch_sqlite_error_on_init_db_returns_rc1_with_diagnostic(
        tmp_path, capsys, monkeypatch):
    """cli.init_db が sqlite3.Error を起こした場合、生の traceback ではなく
    診断メッセージ + rc=1 に正規化される (codex I-2: DB 初期化境界)。"""
    import argparse
    import sqlite3

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli, "init_db", boom)
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"):
        args = argparse.Namespace(command="history", history_command="coverage",
                                  symbol="USDJPY", timeframe="1h",
                                  source="dukascopy", base_interval="1m",
                                  **{"from": "2026-07-01", "to": "2026-07-02"})
        rc = cli.dispatch(args, tmp_path)
    assert rc == 1
    assert "disk I/O error" in capsys.readouterr().err


# ---- 既定サービス動作の不変性 (上書き節 A) --------------------------------


def test_cli_default_service_behavior_unchanged(monkeypatch):
    with patch("agentic_fx.service.run_service", return_value=0) as rs:
        rc = main([])
    assert rc == 0 and rs.called


def test_backtest_run_rejects_live_source(tmp_path, monkeypatch, capsys):
    """人間 CLI にライブ source を渡すと argparse が fail closed する
    (設計書 D2)。SystemExit(2) は argparse の標準的な引数エラー終了コード。

    段 0 の変異 M16-16 (`choices=sorted(ohlcv.IMPORT_SOURCES)` の削除) は
    exit code だけを見る版では**生存した** — choices が無くても後段の
    初期化ガードが同じ SystemExit(2) を出すため。拒否した主体が argparse の
    allowlist であることまで見る。
    """
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "run", "--symbol", "USDJPY", "--source", "yfinance",
             "--from", "2026-01-01", "--to", "2026-01-02",
             "--proposal-file", "dummy.jsonl"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_history_coverage_rejects_live_source(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["history", "coverage", "--symbol", "USDJPY",
             "--timeframe", "1h", "--source", "mt5-live",
             "--from", "2026-01-01", "--to", "2026-01-02"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_analyze_corr_rejects_live_source(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "corr", "--a", "USDJPY", "--b", "EURUSD",
             "--timeframe", "1h", "--source", "twelvedata"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_improve_verify_backend_routes_to_verify_backend(tmp_path, monkeypatch):
    """`_BACKTEST_COMMANDS`/`register_subparsers`/`dispatch` のルーティング
    だけを pin する。実 backend へは一切到達しない (外向きリクエスト予算 —
    メモリ outbound-request-budget-is-a-design-constraint)。"""
    from unittest.mock import patch
    from agentic_fx.entry import main as entry_main
    from agentic_fx.loops.verify_backend import VerifyBackendResult
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.loops.verify_backend.verify_backend") as vb:
        vb.return_value = VerifyBackendResult(
            ok=True, backend="local", provider=None,
            fingerprint="0" * 64, detail="ok")
        rc = entry_main(["improve", "verify-backend", "--backend", "local"])
    assert rc == 0
    assert vb.call_args.kwargs["backend"] == "local"
    assert vb.call_args.kwargs["provider"] is None


def test_cli_improve_verify_backend_routes_opencode_through_to_verify_backend(
        tmp_path, monkeypatch):
    """F-C3 是正 (段0 診断4): 上の routing テストは `--backend local`
    (provider 未指定) の 1 ケースしか流していなかったため、
    backend を codex 固定にする変異を防ぐため、opencode が
    `verify_backend` へ実際に届くことを見る。"""
    from unittest.mock import patch
    from agentic_fx.entry import main as entry_main
    from agentic_fx.loops.verify_backend import VerifyBackendResult
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.loops.verify_backend.verify_backend") as vb:
        vb.return_value = VerifyBackendResult(
            ok=True, backend="opencode", provider=None,
            fingerprint="0" * 64, detail="ok")
        rc = entry_main(["improve", "verify-backend", "--backend", "opencode"])
    assert rc == 0
    assert vb.call_args.kwargs["backend"] == "opencode"
    assert vb.call_args.kwargs["provider"] is None


def test_cli_verify_backend_prints_enable_guidance_for_opencode(
        tmp_path, monkeypatch, capsys):
    """I1 是正 (codex 1周目, verified-codex-round1.md `cli.py:533` 脚):
    opencode の成功後にのみ verify フラグの有効化案内が出ることを pin する。"""
    from unittest.mock import patch
    from agentic_fx.entry import main as entry_main
    from agentic_fx.loops.verify_backend import VerifyBackendResult
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.loops.verify_backend.verify_backend") as vb:
        vb.return_value = VerifyBackendResult(
            ok=True, backend="opencode", provider=None,
            fingerprint="0" * 64, detail="ok")
        rc = entry_main(["improve", "verify-backend", "--backend", "opencode"])
    assert rc == 0
    assert vb.call_args.kwargs["provider"] is None
    assert "llama_swap_verified" in capsys.readouterr().out


def test_cli_verify_backend_prints_detail_and_fingerprint_on_success(
        tmp_path, monkeypatch, capsys):
    """L-F13 是正 (verified-local-round1.md §1【7】): 設計 §7.2「成功時に
    fingerprint を出力し、それが人間の llama_swap_verified 判断の根拠に
    なる」という因果の pin。`print(result.detail)` /
    `print(f"fingerprint: {result.fingerprint}")` を丸ごと消す変異が
    既定スイートで無防備だった (`grep -rn 'fingerprint:' tests/` = 0 件)。"""
    from unittest.mock import patch
    from agentic_fx.entry import main as entry_main
    from agentic_fx.loops.verify_backend import VerifyBackendResult
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    fp = "a" * 64
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.loops.verify_backend.verify_backend") as vb:
        vb.return_value = VerifyBackendResult(
            ok=True, backend="codex", provider="chatgpt",
            fingerprint=fp, detail="backend=codex provider=chatgpt model=m")
        rc = entry_main(["improve", "verify-backend", "--backend", "codex",
                        "--provider", "chatgpt"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "backend=codex provider=chatgpt model=m" in out
    assert f"fingerprint: {fp}" in out


def test_cli_improve_verify_backend_returns_1_when_verification_fails(
        tmp_path, monkeypatch, capsys):
    """F-C1 是正 (段0 致命2、最重要): `if not result.ok: ... return 1` の
    `return 1` を `return 0` に変える変異は、既存 routing テストが
    `ok=True` の 1 ケースしか流していなかったため生存していた
    (メモリ §6.11「fixture が一様だと条件分岐の片側が一度も踏まれない」)。
    検証専用 CLI の終了コードは契約そのもの (runbook 側は
    `afx improve verify-backend ... && ...` の形で判定する) — `ok=False`
    側で `rc == 1` かつ stderr に `result.detail` が出ることを固定する。"""
    from unittest.mock import patch
    from agentic_fx.entry import main as entry_main
    from agentic_fx.loops.verify_backend import VerifyBackendResult
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.loops.verify_backend.verify_backend") as vb:
        vb.return_value = VerifyBackendResult(
            ok=False, backend="local", provider=None,
            fingerprint=None, detail="mission did not complete: status=failed")
        rc = entry_main(["improve", "verify-backend", "--backend", "local"])
    assert rc == 1
    assert "mission did not complete: status=failed" in capsys.readouterr().err


def test_cli_verify_backend_reports_ready_mismatch_as_rc1_not_traceback(
        tmp_path, monkeypatch, capsys):
    """L-F1/L-F14 是正 (verified-local-round1.md §1【1】【8】、裁定 C 案):
    親ゲート (a) (`on_ready` の run_context mismatch) は他の 3 ゲート
    (b)(c)(d) と異なり `VerifyBackendResult(ok=False, ...)` を返さず
    生の `RuntimeError` を送出していたため、`dispatch` の統一エラー境界
    (`except (ValueError, KeyError, OSError, sqlite3.Error)`) を素通りして
    人間に生 traceback が出ていた。専用例外 `VerifyBackendGateError` へ
    変え、`dispatch` の except にその型を足したことで、他の 3 ゲート同様
    `エラー: ...` + rc=1 に畳まれることを固定する。"""
    from unittest.mock import patch
    from agentic_fx.entry import main as entry_main
    from agentic_fx.loops.verify_backend import VerifyBackendGateError
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.loops.verify_backend.verify_backend") as vb:
        vb.side_effect = VerifyBackendGateError(
            "verify-backend: ready frame run_context mismatch (expected "
            "mission_id=-1 ...)")
        rc = entry_main(["improve", "verify-backend", "--backend", "local"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "run_context mismatch" in err
    assert "Traceback" not in err


def test_cli_improve_verify_backend_requires_backend_argument(
        tmp_path, monkeypatch, capsys):
    """F-C2 是正 (段0 診断4): `--backend` の `required=True` → `False` の
    変異は、既存テストが `--backend` を常に渡していたため生存していた。
    `--backend` を省略すると argparse が rc=2 で拒否することを固定する
    (省略時に `backend=None` が `verify_backend` へ渡り、Literal 契約
    違反が `build_launcher_argv` まで落ちてから初めて死ぬ退行を防ぐ)。"""
    from agentic_fx.entry import main as entry_main
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        entry_main(["improve", "verify-backend"])
    assert exc.value.code == 2
    assert "--backend" in capsys.readouterr().err

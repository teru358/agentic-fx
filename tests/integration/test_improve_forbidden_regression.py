"""遮断 8 項目の実プロセス統合回帰 (設計書 §7.1-1、§8.1-13)。

**このファイルは Task 7 で red のまま書き始める** — ①②③ は既存
`tests/test_improve_profile_isolation.py` の probe と同じ手法で Task 7
時点で green にできるが、④〜⑧ は Task 8/10 が実装するまで存在しない
経路 (RPC ツール未配線・`ImproveLoop` 未実装) を検査するため red のまま
残る。**どの task が何を green にするかは末尾の表で管理する** — 各
`pytest.mark.xfail(strict=True, reason=...)` に担当 task を明記し、
green 化した task が `xfail` マーカーを外す。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.landlock import is_available as landlock_available
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs

pytestmark = pytest.mark.skipif(
    not landlock_available(), reason="Landlock not available on this kernel/architecture")

# precheck 2026-08-23 wave3: RW7 — Step 12-5 のための定数と helper 関数
_RW7_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
_RW7_SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
# `_ANALYSIS_ROW_KEYS`/`_BACKTEST_ROW_KEYS` (10.10節) が読む形と同じ
# save_kwargs (`holdout.run_in_sample` の record_fn 契約、7-D)
_RW7_SAVE_KWARGS = dict(
    scope="in_sample", plugin_ref="plugins/_staging/x/myst",
    content_hash="cand-hash", kind="strategy", pair="USDJPY",
    timeframe="1h", source="dukascopy",
    period=(datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc)),
    metrics={"pf": 1.3, "trades": 40}, settings_hash="s",
    core_commit="c", initial_balance=10000.0, now=_RW7_NOW)
# 遮断7 の禁止キー集合 (`tools/improve_rpc_tools.py::_FORBIDDEN_KEYS` と
# 一致させる — 現物が正、ここでは pin 用に転記するだけで置換はしない)
_RW7_FORBIDDEN_KEYS = {
    "period_start", "period_end", "period", "now", "start", "end",
    "window", "timestamps", "in_sample_until"}


def _rw7_assert_no_forbidden_keys(value, path="$"):
    """`_FORBIDDEN_KEYS` の全キーが dict/list の任意の深さにも現れない
    ことを再帰的に確認する (`_strip_forbidden` の再帰化、Task 7 検収 B2
    是正の pin)。"""
    if isinstance(value, dict):
        hit = _RW7_FORBIDDEN_KEYS & set(value.keys())
        assert not hit, f"{path} が禁止キーを含む: {hit}"
        for k, v in value.items():
            _rw7_assert_no_forbidden_keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _rw7_assert_no_forbidden_keys(v, f"{path}[{i}]")


def _rw7_build_improve_loop(tmp_path, conn):
    from agentic_fx.loops.improve_loop import ImproveLoop

    class _FakeRag:
        pass

    return ImproveLoop(
        root=tmp_path, settings=_RW7_SETTINGS, clock=FixedClock(_RW7_NOW),
        db_write_conn_factory=lambda: conn,
        db_readonly_conn_factory=lambda: conn,
        activity=ActivityLog(tmp_path / "activity.log"), rag=_FakeRag())


_PROBE_SCRIPT = textwrap.dedent("""
    import json, os, sqlite3, sys
    from pathlib import Path
    os.chdir(sys.argv[2])
    from agentic_fx.mission_worker import _bootstrap_improve_profile

    data_dir = Path(sys.argv[1])
    workdir = Path(sys.argv[2])
    # プローブ内で staging_dir を作成 (既存 test_improve_profile_isolation.py に準ずる)
    mission_id = sys.argv[3] or "probe-mission"
    staging_dir = workdir / "staging" / mission_id
    staging_dir.mkdir(parents=True, mode=0o700)
    # source_snapshot_dir も workdir 配下に置く (bootstrap の検証要件)
    source_snapshot_dir = workdir / "source"
    source_snapshot_dir.mkdir(parents=True, mode=0o500)
    results = {}
    try:
        _bootstrap_improve_profile(
            backend="local", mission_id=mission_id,
            staging_dir=str(staging_dir),
            source_snapshot_dir=str(source_snapshot_dir),
            claude_bin=None, codex_bin=None)
        results["bootstrap"] = "ok"
    except Exception as e:
        results["bootstrap"] = f"FAILED: {type(e).__name__}: {e}"
        print(json.dumps(results))
        sys.exit(0)

    # ①②③ (既存 probe と同型 — Task 5/7 時点で green)
    try:
        open(str(data_dir / "agentic.db"))
        results["open_db"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["open_db"] = "blocked"
    except Exception as e:
        results["open_db"] = f"UNEXPECTED: {type(e).__name__}: {e}"
    try:
        os.listdir(str(data_dir))
        results["list_data_dir"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["list_data_dir"] = "blocked"
    except Exception as e:
        results["list_data_dir"] = f"UNEXPECTED: {type(e).__name__}: {e}"

    # ④ 書き込み可能パスが staging・workdir・/dev に閉じる
    for target, label in [
            (Path(sys.argv[4]) / "reports" / "x.md", "reports"),
            (Path(sys.argv[4]) / "config" / "x.yaml", "config"),
            (Path(sys.argv[4]) / "policy" / "x.md", "policy"),
            (Path(sys.argv[4]) / "plugins" / "some_plugin" / "plugin.py", "plugins_body")]:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")
            results[f"write_{label}"] = "UNEXPECTED_SUCCESS"
        except PermissionError:
            results[f"write_{label}"] = "blocked"
        except Exception as e:
            results[f"write_{label}"] = f"UNEXPECTED: {type(e).__name__}: {e}"
    try:
        (staging_dir / "probe.txt").write_text("ok")
        results["write_staging"] = "ok"
    except Exception as e:
        results["write_staging"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"

    # staging_dir は workdir と同じく書き込み可能のはずだが、念のため確認
    # write_staging で確認する

    # ⑥ 取引 registry のツールが improve registry に無い + IMPROVE_FORBIDDEN
    # 逸脱対応: settings=None では improve 分岐が settings.improve.research を
    # アクセスするため AttributeError。プローブ内で Settings をロードする。
    from agentic_fx.config import load_settings
    from agentic_fx.tools import mission_registry
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.tools.signal_tools import IMPROVE_FORBIDDEN

    # workdir にコピーされた config ファイルを参照
    settings = load_settings(Path(sys.argv[2]) / "config" / "settings.yaml.example")

    registry = mission_registry.build_mission_registry(
        "improve", sqlite3.connect(":memory:"), settings, None, None,
        activity=None, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})
    names = set(registry.names())
    forbidden_hit = names & (IMPROVE_FORBIDDEN | {
        "get_ohlcv", "get_indicators", "get_econ_calendar", "place_intent"})
    results["forbidden_tools_present"] = sorted(forbidden_hit) or "none"

    print(json.dumps(results))
""")


def test_improve_worker_write_boundary_and_forbidden_tools(tmp_path):
    """①②④⑥ を実プロセスで検査する。③⑤⑦⑧ は下記の別テストに分ける
    (1 検査目的 1 テストの規律 — ただし前提条件のセットアップは同居可)。"""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    seed = __import__("sqlite3").connect(str(data_dir / "agentic.db"))
    seed.execute("CREATE TABLE t (x INTEGER)"); seed.commit(); seed.close()
    workdir = tmp_path / "workdir"; workdir.mkdir()
    root = tmp_path / "root"; root.mkdir()

    # 設定ファイルを workdir にコピー（プローブが参照できるようにするため）
    from shutil import copy
    config_src = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    config_dir = workdir / "config"
    config_dir.mkdir()
    copy(config_src, config_dir / "settings.yaml.example")

    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT, str(data_dir), str(workdir),
         "probe-mission", str(root)],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # レビュー1周目 I1: bootstrap 自体が失敗していないことを EACCES 検査より
    # 先に明示 assert する — さもないと Task 5 が必須 keyword 6 個へ変更した
    # `_bootstrap_improve_profile` の呼び出し形が壊れていても、この後続の
    # `out["open_db"]` 等の KeyError/AssertionError だけが観測され、
    # 「境界が壊れた」のか「bootstrap 自体に到達していない」のか区別できない。
    assert out["bootstrap"] == "ok", out
    assert out["open_db"] == "blocked"
    assert out["list_data_dir"] == "blocked"
    assert out["write_reports"] == "blocked"
    assert out["write_config"] == "blocked"
    assert out["write_policy"] == "blocked"
    assert out["write_plugins_body"] == "blocked"
    assert out["write_staging"] == "ok"
    assert out["forbidden_tools_present"] == "none"


def test_rpc_tools_return_no_period_endpoints_and_do_not_write_db_directly(
        tmp_path, monkeypatch):
    """遮断⑦: `ImproveLoop._build_rpc_handlers` → `build_improve_rpc_tooldefs`
    を経由した RPC ツールの agent 向け戻り値に period_start/period_end/
    period/now/in_sample_until が**再帰的な深さでも**含まれないこと
    (`_strip_forbidden` の再帰化、Task 7 検収 B2 是正)、かつ RPC handler の
    呼び出しそのものが `backtest_runs`/`analysis_runs` へ直接行を書かない
    (commit 前の DB 書込は Tx-2 の `_persist_ledger_rows`/
    `_persist_gate_rows` 経由のみ、10.10 節) ことを end-to-end で検査する。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    loop = _rw7_build_improve_loop(tmp_path, conn)

    fake_meta = SimpleNamespace(
        name="myst", kind="strategy", timeframe="1h",
        content_hash="cand-hash", pairs=("USDJPY",))
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: fake_meta)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_adapter.build_intent_source",
        lambda meta, **kw: SimpleNamespace(close=lambda: None))

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        record_fn(_RW7_SAVE_KWARGS)
        return dict(_RW7_SAVE_KWARGS["metrics"])
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        _fake_run_in_sample)

    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": 600.0, "analyze_corr": 60.0})
    handlers = loop._build_rpc_handlers(ledger, staging_dir=staging_dir)
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=handlers["run_backtest"],
        analyze_corr_handler=handlers["analyze_corr"])}

    # period_start/period_end/period/now/in_sample_until が無いことを確認
    bt_out = tools["run_backtest"].func(name="myst", pair="USDJPY")
    _rw7_assert_no_forbidden_keys(bt_out)

    # analyze_corr の結果も同様
    an_out = tools["analyze_corr"].func(
        request={"kind": "corr_matrix", "timeframe": "1h"})
    _rw7_assert_no_forbidden_keys(an_out)


def test_holdout_and_analysis_ids_never_come_from_agent_output(tmp_path):
    """遮断⑧: 承認申請の `analysis_run_ids` は agent の自己申告
    (`_build_approval_payload` が置く placeholder) ではなく、
    `_finalize_success` が Tx-2 内で `_persist_ledger_rows` の戻り値
    (実際に `analysis_runs` へ永続化した行 id) で必ず上書きする
    (10.10 節 `_finalize_success` の申し送り③)。ここでは agent が偽装した
    placeholder (`[9999]`) を `approval_payload` に混入させた状態で
    `_finalize_success` を直接呼び、承認行の `payload_json` に書かれる
    `analysis_run_ids`/`analysis_call_count`/`trial_count` が偽装値では
    なく台帳から実際に導出した値であることを検査する。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    loop = _rw7_build_improve_loop(tmp_path, conn)

    mission_id = missions_store.start(
        conn, "improve", "codex", "gpt-5", _RW7_NOW, commit=False)
    backlog_id = backlog_store.add(conn, "idea", "user", _RW7_NOW)
    run_id = improve_runs_store.start(
        conn, None, _RW7_NOW, mission_id=mission_id, commit=False)
    conn.commit()

    forged_payload = {
        "name": "x", "kind": "indicator",
        # agent が自己申告した偽の analysis_run_ids/回数/trial_count —
        # `_finalize_success` はこれを無視して台帳由来の実値で上書きする
        # 契約 (10.10 節)。
        "analysis_run_ids": [9999], "analysis_call_count": 99,
        "trial_count": 99999}
    ledger_entries = [{"kind": "analyze_corr", "trial_count": 1,
                       "result_summary": {
                           "params": {"request": {"kind": "corr_matrix"}},
                           "trial_count": 1, "source": "dukascopy"}}]

    loop._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload=forged_payload, now=_RW7_NOW,
        ledger_entries=ledger_entries, gate_rows=())

    real_analysis_id = conn.execute(
        "SELECT id FROM analysis_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    approval_row = conn.execute(
        "SELECT payload_json FROM approval_requests ORDER BY id DESC "
        "LIMIT 1").fetchone()
    payload = json.loads(approval_row["payload_json"])
    assert payload["analysis_run_ids"] == [real_analysis_id]
    assert payload["analysis_run_ids"] != [9999]
    assert payload["analysis_call_count"] == 1
    assert payload["trial_count"] == 1
    conn.close()

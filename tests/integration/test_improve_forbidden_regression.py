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
from pathlib import Path

import pytest

from agentic_fx.core.landlock import is_available as landlock_available

pytestmark = pytest.mark.skipif(
    not landlock_available(), reason="Landlock not available on this kernel/architecture")

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


@pytest.mark.xfail(strict=True, reason=
    "遮断⑦: run_backtest/analyze_corr の RPC ハンドラは Task 10 の "
    "ImproveRunContext.rpc_handlers 配線が無いと呼べない。Task 10 で green 化。")
def test_rpc_tools_return_no_period_endpoints_and_do_not_write_db_directly():
    pytest.fail("Task 10 待ち — RunContext.rpc_handlers が未配線")


@pytest.mark.xfail(strict=True, reason=
    "遮断⑧: holdout 指標・baseline 差分・閾値別合否は ImproveLoop の commit 相 "
    "(Task 10) が生成する。payload の analysis id/回数が RPC 台帳由来である "
    "ことも Task 10 の finish_improve_mission 経由でしか検証できない。")
def test_holdout_and_analysis_ids_never_come_from_agent_output():
    pytest.fail("Task 10 待ち — commit 相未実装")

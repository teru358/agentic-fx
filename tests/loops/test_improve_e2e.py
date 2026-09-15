"""FakeRunner E2E: §7.1-6「発見 → バックログ追加 → 候補 → ゲート → 承認申請 →
レポート公開 → backlog 遷移」の全周を fake で回す (プラン10 Task 12)。

WorkerRunner (実サブプロセス) は monkeypatch で FakeImproveWorkerRunner に
差し替える — Landlock/実 CLI の検証は Task 1〜6 の実プロセステストが担う。
ここでは ImproveLoop の commit 相 (親側ロジック) を対象にする。
"""
# precheck 2026-08-22 wave2: T12-B1 T12-B2 T12-M4
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.runners.base import Mission, MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store.db import connect, connect_readonly

from tests.fixtures.wiring_envs import (
    activity_text as _activity_text,
    improve_env as _improve_env,
    improve_env_with_activity as _improve_env_with_activity,
    mission_for as _mission,
    prepare_ctx as _prepare_ctx,
)
from tests.fixtures.wiring_envs import completed_result as _completed_result
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _no_real_network

_REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)  # Sat 03:00 UTC


class FakeImproveWorkerRunner:
    """`agentic_fx.runners.worker_runner.WorkerRunner` の差し替え double。

    `ImproveLoop.prepare()` が `WorkerRunner(root=..., settings=..., clock=...,
    worker_profile="improve", run_context=ctx, stop_event=...)` の形で構築する
    想定 (Task 10) に合わせ、同じ kwargs を受理し `.run(mission)` だけを
    実装する。"""

    def __init__(self, *, result: MissionResult, **kwargs) -> None:
        self._result = result
        self.kwargs = kwargs
        self.missions: list[Mission] = []

    def run(self, mission: Mission) -> MissionResult:
        self.missions.append(mission)
        return self._result


def _plugin_artifact(name: str, *, kind: str = "indicator",
                     self_test: str = "passed") -> dict:
    return {
        "discoveries": [{"idea": f"idea for {name}", "source": "agent",
                         "evidence": "test evidence", "kind": "task"}],
        "selected": {"backlog_id": None, "idea": f"idea for {name}"},
        "artifact": {"type": "plugin", "name": name, "kind": kind,
                    "self_test": self_test, "summary": f"{name} candidate"},
        "selection_rationale": "test rationale",
    }


def _report_artifact(proposal_kind: str, *, title: str = "test proposal",
                     body_md: str = "test body") -> dict:
    """`{"type": "report", ...}` artifact (§3.5) — `_plugin_artifact` は
    `{"type": "plugin", ...}` しか作れないため、report 系シナリオ用に別ヘルパを
    立てる。"""
    return {
        "discoveries": [],
        "selected": {"backlog_id": None, "idea": f"idea for {title}"},
        "artifact": {"type": "report", "proposal_kind": proposal_kind,
                    "title": title, "body_md": body_md},
        "selection_rationale": "test rationale",
    }


def _write_staging_plugin(staging_dir: Path, name: str,
                          plugin_py: str, config_yaml: str,
                          test_plugin: str) -> Path:
    """FakeImproveWorkerRunner はサブプロセスを起こさないため、`prepare()` が
    作った staging_dir へ候補 3 本を直接置く (worker が書くはずの内容を
    テストが代理で書く)。staging_dir は正規形
    `plugins/_staging/<mission_id>` (D-15 是正、設計書 §2.2/§2.3) である。"""
    candidate = staging_dir / name
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_text(plugin_py, encoding="utf-8")
    (candidate / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (candidate / "test_plugin.py").write_text(test_plugin, encoding="utf-8")
    return candidate


_PASSING_INDICATOR_PY = """
def compute(df, params):
    return {"value": df['close'].iloc[-1]}
"""
_PASSING_INDICATOR_CONFIG = """
kind: indicator
pairs: [USDJPY]
timeframe: 1h
params: {}
"""
_PASSING_INDICATOR_TEST = """
def test_compute_returns_value():
    import pandas as pd
    from plugin import compute
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    assert compute(df, {})["value"] == 3.0

def test_compute_uses_latest_close():
    import pandas as pd
    from plugin import compute
    assert compute(pd.DataFrame({"close": [4.0, 5.0]}), {})["value"] == 5.0

def test_compute_accepts_single_row():
    import pandas as pd
    from plugin import compute
    assert compute(pd.DataFrame({"close": [7.0]}), {})["value"] == 7.0
"""

# strategy kind 用の候補 3 本 (D-15 是正: `test_strategy_below_evaluable_
# min_trades_becomes_observation`/`test_strategy_baseline_falls_back_to_
# no_strategy_row` は `_plugin_artifact(..., kind="strategy")` で agent の
# 申告を strategy にしていたが、実際に staging へ書く候補は
# `_PASSING_INDICATOR_CONFIG` (`kind: indicator`) のままだった — 実装は
# `config.yaml` の `kind` (agent の申告ではなく実体) で strategy ゲートを
# 出し分ける (`_read_candidate_kind`) ため、この乖離により strategy 採用
# ゲート自体が一度も発火せず素通りしていた (逐語乖離の申告)。
# `plugin/loader._KIND_FUNCS["strategy"]` が要求する
# `evaluate(df, indicators, signals, params)` を実装する最小候補。
_PASSING_STRATEGY_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "e2e fixture always holds"}
"""
_PASSING_STRATEGY_CONFIG = """
kind: strategy
pairs: [USDJPY]
timeframe: 1h
exit_mode: levels
params: {}
"""
_PASSING_STRATEGY_TEST = """
def test_evaluate_returns_hold():
    import pandas as pd
    from plugin import evaluate
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    assert evaluate(df, {}, [], {})["action"] == "hold"

def test_evaluate_has_rationale():
    import pandas as pd
    from plugin import evaluate
    result = evaluate(pd.DataFrame({"close": [1.0]}), {}, [], {})
    assert result["rationale"]

def test_evaluate_ignores_optional_inputs():
    import pandas as pd
    from plugin import evaluate
    assert evaluate(pd.DataFrame({"close": [1.0]}), {"x": 1}, ["s"], {"p": 2})["action"] == "hold"
"""

# test_plugin.py が plugin.py の書き換えを試みる不合格候補 (§4.2-3e の主 pin と
# 同じ形をこの E2E でも 1 回なぞる — ゲート不合格 → レポートのみ経路の材料)
_FAILING_INDICATOR_TEST = """
def test_tampers_with_plugin_source():
    with open(__file__.replace('test_plugin.py', 'plugin.py'), 'w') as f:
        f.write('def compute(df, params): return {"value": 999}')
    assert False, 'この候補はゲートで EACCES になるはず'
"""


def _install(root: Path) -> None:
    (root / "config").mkdir()
    src = (_REPO_ROOT / "config" / "settings.yaml.example").read_text(
        encoding="utf-8")
    (root / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(root)


# precheck 2026-08-22 wave2: T12-B6 T12-B8 T12-B9 T12-M1
@pytest.fixture
def improve_env(tmp_path):
    """`build_app` を組み立て、improve レーンを FakeImproveWorkerRunner で
    駆動できる状態にする。取引レーンは本テストの関心外なので `_no_real_network`
    で固定する。`embedding_fn=FakeEmbedding()` / `runner=FakeRunner([])` は
    `tests/test_service_app.py:43-50` の既存 `_seam_app` と同じ規律
    (chromadb のモデル DL を避ける — Task 0)。"""
    _install(tmp_path)
    with _no_real_network():
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
        yield app, tmp_path
        app.close()


def test_full_cycle_discovery_to_backlog_transition(improve_env):
    """§7.1-6 の全周: 発見 → バックログ追加 → 候補 → ゲート合格 → 承認申請
    (pending) → approve → 版 → git 記録 → symlink 切替 → approved → backlog
    が `done` に遷移する、この順序で進むことを確認する。"""
    app, root = improve_env
    conn: sqlite3.Connection = app.conn_core

    result = MissionResult(
        status="completed",
        output=_plugin_artifact("rsi_gate_e2e"),
        transcript=[],
    )

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )

    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "rsi_gate_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    # 承認申請 (pending) が 1 件、payload に candidate_origin/candidate_path
    row = conn.execute(
        "SELECT id, status, payload_json FROM approval_requests WHERE kind='plugin'"
    ).fetchone()
    assert row is not None
    approval_id, status, payload_json = row
    assert status == "pending"
    assert '"candidate_origin": "staging"' in payload_json or \
           "'candidate_origin': 'staging'" in payload_json

    # backlog は selected のまま (承認申請を発行した時点では据え置き)
    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "selected"
    assert backlog_row[1].startswith("approval_pending:")

    # 人間が approve する (P2 経路)
    plugin_switch.approve_candidate(conn, approval_id, decided_by="shell",
                                    now=NOW, plugins_root=root / "plugins",
                                    settings=app.settings)  # wave2-recheck: T11-B1

    approved_row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    assert approved_row[0] == "approved"

    live = root / "plugins" / "rsi_gate_e2e"
    assert live.is_symlink()
    version_dir = live.resolve()
    assert version_dir.is_dir()
    assert (version_dir / "plugin.py").exists()

    backlog_after = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_after[0] == "done"
    assert backlog_after[1] == f"approved:{approval_id}"


def test_oversized_max_bars_becomes_gate_failed_before_strategy_gate(
        improve_env, monkeypatch):
    """改善 loop 固有の承認 corridor も max_bars 上限で fail closed する。

    正常範囲の候補は上の全周テストが pending approval まで進むことで対照を
    固定する。ここでは strategy 候補を使い、strategy gate に届く前に拒否
    されることを直接観測する。
    """
    app, root = improve_env
    conn = app.conn_core
    result = MissionResult(
        status="completed",
        output=_plugin_artifact("oversized_strategy_bars", kind="strategy"),
        transcript=[],
    )
    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )

    def _strategy_gate_must_not_run(*args, **kwargs):
        pytest.fail("max_bars_limit rejection reached the strategy gate")

    monkeypatch.setattr(loop, "_run_strategy_gate", _strategy_gate_must_not_run)
    oversized_config = _PASSING_STRATEGY_CONFIG.replace(
        "params: {}", "max_bars: 999999\nparams: {}")
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        staging_dir = ctx.staging_dir
        _write_staging_plugin(
            staging_dir, "oversized_strategy_bars", _PASSING_STRATEGY_PY,
            oversized_config, _PASSING_STRATEGY_TEST)
        loop.commit(mission=mission, ctx=ctx, result=worker.run(mission), now=NOW)

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 0
    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert backlog[0] == "observation"
    assert "max_bars_limit" in backlog[1]
    assert not staging_dir.exists()
    activity_text = (root / "logs" / "activity.log").read_text()
    assert "max_bars_limit" in activity_text
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_gate_failure_stops_at_report_no_approval_request(improve_env):
    """候補がゲート不合格 (test_plugin.py が plugin.py を書き換えようとする)
    のとき、承認申請は出ず、レポートのみで backlog が observation に落ちる。

    逐語乖離の申告 (着手前検証): `_finalize_gate_failed` は report を
    一切書かずに終端していた (`run_result=None`/`report_state='none'`
    のまま)。`_finalize_loser`/`_prepare_report_if_applicable` は既に
    `<root>/data/improve_reports/improve-<mission_id>.md` へ書く実装が
    3 箇所で揃っている (プラン本文 L20140 の逐語もこの形) ため、D-15 是正
    は `_finalize_gate_failed` にも**同じ既存の規約**でレポート生成を
    追加する — 新しい命名 (`root/"reports"` や日付入りファイル名) は
    導入しない。テスト側のパスをこの既存規約に合わせて直す。"""
    app, root = improve_env
    conn = app.conn_core

    result = MissionResult(
        status="completed",
        output=_plugin_artifact("bad_gate_e2e"),
        transcript=[],
    )
    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "bad_gate_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 0

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1].startswith("gate_failed:")

    report_files = list((root / "data" / "improve_reports").glob("improve-*.md"))
    assert len(report_files) == 1

    live = root / "plugins" / "bad_gate_e2e"
    assert not live.exists()

    # [profitability-floor-fix] G1: 通常の gate_failed (フロア不合格
    # ではない) には適用閾値 3 値を出さない。
    activity_text = (root / "logs" / "activity.log").read_text()
    gate_failed_lines = [
        line for line in activity_text.splitlines() if "gate_failed" in line]
    assert gate_failed_lines
    assert not any("min_pf=" in line for line in gate_failed_lines)


def test_gate_failure_stops_before_any_further_candidate_processing(
        improve_env, monkeypatch):
    """F-E1 是正 (段0 最優先申し送り): `improve_loop.py:1179-1183`
    (`if not gate_verdict.passed: self._finalize_gate_failed(...); return`)
    の早期 `return` を削除する変異は `test_gate_failure_stops_at_report_
    no_approval_request` で KILLED になっていたが、実測 (`--tb=line`) では
    6 件全てが `_finalize_gate_failed` 先頭の `self._delete_staging(ctx)`
    で staging が消えた直後、後続へ流れた `self._read_candidate_kind`
    (`improve_loop.py:1333`) の `FileNotFoundError` という**巻き添え**で
    落ちていた — 遮断そのものの assert には一度も到達していない
    (メモリ「『死ぬ理由が他にある』と pin は恒真になる」2026-08-22 A-4)。

    着手前検証: 当初 `_delete_staging` を no-op にして
    `approval_count == 0` を見る形を試みたが、実測すると
    `_finalize_success` 側の `finish_improve_mission` が「既に
    `_finalize_gate_failed` で終端済みの mission/run」に対して二重に
    終端しようとして Tx-2 が失敗 → 内側 tx が rollback → 一度挿入された
    approval_requests 行ごと巻き戻る、という**別の巻き添え** (二重終端の
    tx 補償) 経由で偶然 `approval_count == 0` のまま緑になることが分かった
    (状態ベースの assert はこの種の間接効果を区別できない)。

    そこで手段を変え、「ゲート不合格の直後は後続の候補処理
    (`_read_candidate_kind` 以降) に一切進まないこと」そのものを spy で
    直接固定する — `_delete_staging`/Tx-2 のどちらの巻き添えにも依存しない。
    早期 `return` が削除されると `_read_candidate_kind` が (staging の
    生死に関わらず) 呼ばれてしまうため、この spy は単独で red になる。"""
    from agentic_fx.loops.improve_loop import ImproveLoop

    called = {"read_candidate_kind": False}
    orig_read_candidate_kind = ImproveLoop._read_candidate_kind

    def _spy_read_candidate_kind(self, candidate_dir):
        called["read_candidate_kind"] = True
        return orig_read_candidate_kind(self, candidate_dir)

    monkeypatch.setattr(ImproveLoop, "_read_candidate_kind",
                        _spy_read_candidate_kind)

    app, root = improve_env
    conn = app.conn_core

    result = MissionResult(
        status="completed",
        output=_plugin_artifact("bad_gate_e2e_no_further_processing"),
        transcript=[],
    )
    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "bad_gate_e2e_no_further_processing",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    assert called["read_candidate_kind"] is False, (
        "gate 不合格の直後に候補処理 (_read_candidate_kind 以降) が進んだ "
        "(F-E1 の早期 return が崩れている)")

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 0, (
        "gate 不合格の候補から approval_request が作られた (F-E1 の遮断が "
        "崩れている)")


def test_landlock_unavailable_produces_gate_failure_not_approval(
        improve_env, monkeypatch):
    """D20 是正 (段0 致命3): `run_gate_pytest` が Landlock 不可の
    fail-closed `RuntimeError` (設計書 §4.2-3d) を投げたとき、candidate の
    pytest は一度も実行されなかったにもかかわらず `passed=True` へ化けて
    approval_requests に承認待ちとして載ってはならない
    (CLAUDE.md「テスト不合格の変更は approval_request 化禁止」)。

    候補自体は `test_full_cycle_discovery_to_backlog_transition` と同じ
    正常系の内容 (gate に通れば approval まで進む形) にし、
    `run_gate_pytest` だけを RuntimeError で差し替える —
    `_run_plugin_gate` の戻り値だけでなく `commit()` を最後まで通して
    `approval_requests` の件数まで踏む (§ 採ってはいけない案 の回避)。"""
    app, root = improve_env
    conn = app.conn_core

    result = MissionResult(
        status="completed",
        output=_plugin_artifact("landlock_gate_e2e"),
        transcript=[],
    )
    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )

    def _raise_landlock_unavailable(plugin_dir, *, settings):
        raise RuntimeError("landlock unavailable")

    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        _raise_landlock_unavailable)

    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "landlock_gate_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 0

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1].startswith("gate_failed:")


# precheck 2026-08-22 wave2: T12-M2
def test_concurrent_duplicate_selection_loser_becomes_observation(improve_env):
    """2 Mission が同じ backlog id を同時に選択したとき、Tx-1 の CAS に
    負けた側 (後着) が observation に落ち、勝者だけがゲートへ進む
    (§4.1 Tx-1、§8.1-24)。M-2 (report-task12.md): 旧版は同一接続で逐次 2 回
    `loop.prepare`/`commit` を呼ぶだけで「並行」を名乗っておらず、
    `backlog_row` を fetch しても未使用のまま、killer は目視確認頼みだった。
    ここでは 2 スレッド + 別接続 (`db_write_conn_factory` が呼び出しごとに
    新しい接続を作る現物契約を利用) を使い、`loop.commit()` 呼び出し直前で
    `threading.Barrier(2)` により実際に競合させる。

    着手前検証で発見した実バグの是正 (1815 errors の根本原因): 旧版は
    `unittest.mock.patch("…WorkerRunner", …)` を**各スレッドの内側**で
    個別に `with` していた。`patch.__enter__`/`__exit__` は同一ターゲット
    への並行呼び出しに対して原子的でない (各スレッドが `__enter__` 時点の
    値を「元の値」として捕まえ、`__exit__` で早く終わった方がそれを書き
    戻すため、遅い方の `__exit__` が最終的にどちらか一方のスレッド用
    lambda を `agentic_fx.runners.worker_runner.WorkerRunner` に永久に
    書き戻してしまう)。この 1 テストの実行だけで `WorkerRunner` が
    プロセス終了までクラス→関数に化け、以降の**全テスト**の
    `tests/conftest.py::_forbid_worker_spawn_against_real_llama_swap`
    autouse fixture が `WorkerRunner.run` の属性アクセスで
    `AttributeError` になり、スイート全体が芋づる式に ERROR になっていた
    (`pytest tests/loops/test_improve_e2e.py::test_concurrent_duplicate_selection_loser_becomes_observation
    tests/tools/test_registry.py::test_register_and_names` で単独再現・
    確認済み)。patch はメインスレッドで**両スレッドの起動〜join を囲む
    1 回だけ**に統一し、結果はスレッド名で振り分ける。"""
    import threading

    app, root = improve_env
    conn = app.conn_core

    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)

    def _selected_output(name):
        out = _plugin_artifact(name)
        out["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}
        return out

    results = {
        name: MissionResult(status="completed",
                            output=_selected_output(name), transcript=[])
        for name in ("winner_e2e", "loser_e2e")
    }

    barrier = threading.Barrier(2)
    mission_ids: dict[str, int] = {}
    errors: list[BaseException] = []

    def _worker_runner_factory(**kw):
        # スレッド名で対応する fake 結果を振り分ける (Thread(name=...) で
        # 設定する)。単一 patch を両スレッドで共有するため、ここは
        # 読取専用の dict 参照だけで完結し競合しない。
        name = threading.current_thread().name
        return FakeImproveWorkerRunner(result=results[name], **kw)

    def _run_one(name: str) -> None:
        try:
            loop = ImproveLoop(
                root=root, settings=app.settings, clock=FixedClock(NOW),
                db_write_conn_factory=lambda: connect(
                    root / "data" / "agentic.db"),
                db_readonly_conn_factory=lambda: connect_readonly(
                    root / "data" / "agentic.db"),
                activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
            )
            mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
            _write_staging_plugin(
                ctx.staging_dir, name,
                _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
                _PASSING_INDICATOR_TEST)
            mission_ids[name] = ctx.mission_id
            mission_result = worker.run(mission)
            barrier.wait(timeout=10)  # 両スレッドの commit を競合させる
            loop.commit(mission=mission, ctx=ctx, result=mission_result,
                       now=NOW)
        except BaseException as exc:  # noqa: BLE001 — スレッド内例外を回収
            errors.append(exc)

    threads = [threading.Thread(target=_run_one, args=(name,), name=name)
              for name in results]
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               _worker_runner_factory):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert errors == [], f"thread(s) raised: {errors!r}"

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    # 二重の承認申請が無いことが本テストの主 killer。3(iii) 是正: 旧稿は
    # `<= 1` だったため両スレッドが失敗して 0 件でも素で pass する弱い
    # assert だった (CAS 敗者経路を実際に検証していない)。勝者は必ず
    # ゲートを通って承認申請を 1 件出すはずなので `== 1` に強化する。
    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 1
    # M-2 で未使用のまま残っていた backlog_row を実際に使う。逐語乖離の
    # 申告 (着手前検証): 旧稿は「`selected` のまま止まらないこと」を
    # 期待していたが、これは §4.3 状態表と矛盾する — 勝者がゲートを通り
    # 承認申請 (pending) を出す経路は「selected (据え置き)」が正しい遷移
    # であり (`_finalize_success` の `backlog_transition`)、敗者は backlog
    # に一切触れない (`_finalize_loser` の docstring 参照)。実際に競合が
    # 起きた/起きなかったの判定基準は「selected かどうか」ではなく
    # `last_result` が `approval_pending:<id>` になっているか (= 勝者の
    # Tx-2 が実行された証跡) で行う。
    assert backlog_row[0] == "selected"
    assert backlog_row[1] is not None
    assert backlog_row[1].startswith("approval_pending:")

    # 3(iii) 是正: 勝者・敗者それぞれの mission が実際に別の終端状態へ
    # 落ちたことを直接確認する (CAS 敗者経路そのものの実測)。勝者は
    # ゲート通過 (approval_pending)、敗者は Tx-1 CAS 負けの「重複のため
    # 見送り」レポート終端 (`result='report'`) — どちらの mission_id が
    # 勝者/敗者になるかは Barrier のタイミング依存で非決定なので、
    # 集合として検証する。
    run_rows = {
        name: conn.execute(
            "SELECT result FROM improvement_runs WHERE mission_id=?",
            (mission_ids[name],)).fetchone()
        for name in results
    }
    run_results = sorted(row["result"] for row in run_rows.values())
    assert run_results == ["approval", "report"], (
        "勝者 (承認申請発行, result='approval') と敗者 "
        "(重複見送りレポート, result='report') の 1 組にならなかった: "
        f"{run_results!r}")


_MISSION_MAX_TURNS_ATTR = "improve.mission_max_turns"  # 参考: §7.1-2 対応表脚注


# precheck 2026-08-22 wave2: T12-B4
def _fake_record_kwargs(*, settings, trades: int, **overrides) -> dict:
    """現物 `src/agentic_fx/backtest/holdout.py:113-133` `_run_scope` が
    `record_fn(save_kwargs)` へ渡す `save_kwargs` (= `save_harness_run` の
    kwargs) の形を組み立てる。`variant` キーは現物 `save_kwargs` に含まれない
    — trades 数は `metrics` に埋める (§4.2-4 の evaluable 判定は呼び出し側の
    `trades >= EVALUABLE_MIN_TRADES` が別途行うため、`metrics` の内容自体は
    このテストの関心外)。呼び出し側 (`run_in_sample`/`run_holdout_gate` の実
    引数) から `plugin_ref`/`content_hash`/`kind`/`pair`/`eval_timeframe`/
    `source`/`period_start`/`period_end`/`now` が渡ってくる想定だが、Task 10
    未実装の本ファイル執筆時点では正確な呼び出しシグネチャが確定していない
    ため `overrides` (fake `_run` が受け取った **kwargs から抽出した値) で
    上書きしつつ、欠けている項目には安全なデフォルトを補う (申し送り 9②)。"""
    dataset = overrides.get("dataset")
    source = overrides.get("source", dataset.source if dataset is not None
                           else "yfinance")
    base_interval = overrides.get(
        "base_interval", dataset.base_interval if dataset is not None else "1m")
    return dict(
        scope=overrides.get("scope", "in_sample"),
        plugin_ref=overrides.get("plugin_ref", "unknown:unknown"),
        content_hash=overrides.get("content_hash", "0" * 64),
        kind=overrides.get("kind", "strategy"),
        pair=overrides.get("pair", "USDJPY"),
        timeframe=overrides.get("eval_timeframe", overrides.get("timeframe", "1h")),
        source=source, base_interval=base_interval, params={},
        period=(overrides.get("period_start", NOW), overrides.get("period_end", NOW)),
        # [profitability-floor] T2 Step 2-1 (2026-09-13): `run_backtest_
        # handler` はこの `metrics` に対して `_check_profitability_floor`
        # を直接呼ぶ (`save_kwargs["metrics"]` が判定入力) — `pf`/`avg_r`
        # を直接インデックスするため、完全な metrics 形状にする
        # (既定はフロアを通す値、既存テストの期待値は変えない)。
        metrics={"trades": trades, "pf": overrides.get("pf", 1.5),
                "avg_r": overrides.get("avg_r", 0.1),
                "evaluable": trades >= 30},
        settings_hash="fake-settings-hash",
        core_commit="fake-core-commit",
        initial_balance=settings.backtest.initial_balance,
        now=overrides.get("now", NOW),
    )


def _fake_in_sample_metrics(trades: int, *, pf: float | None = 1.5,
                            avg_r: float | None = 0.1):
    """`run_in_sample(settings, *, history_conn, record_fn, ...)` の
    fake。§4.2-4 の evaluable 判定 (`trades >= EVALUABLE_MIN_TRADES`, 既存
    `backtest/metrics.py`) に合わせ、`record_fn` へ現物 `save_kwargs` 形の
    in_sample 行を 1 つ積んでから
    `{"trades": trades, "pf": pf, "avg_r": avg_r, "evaluable": trades >=
    30}` を返す。**[profitability-floor] T1 Step 1-2 (2026-09-12)**:
    `pf`/`avg_r` の既定はフロアを通す値 (`pf>=1.0`, `avg_r>0`) —
    `_check_profitability_floor` が `m["pf"]`/`m["avg_r"]` を直接
    インデックスするため (`.get` に緩めない、実 metrics 形状に揃える —
    メモリ `test-fixtures-from-real-transcripts`)。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        record_fn(_fake_record_kwargs(settings=settings, trades=trades,
                                      scope="in_sample", pf=pf, avg_r=avg_r,
                                      **kwargs))
        return {"trades": trades, "pf": pf, "avg_r": avg_r,
                "evaluable": trades >= 30}
    return _run


def _fake_holdout_metrics(*, pf: float | None = 1.5, avg_r: float | None = 0.1):
    """`run_holdout_gate` の fake。`record_fn` へ現物 `save_kwargs` 形の
    holdout 行を積む。①のシナリオ (trades<30) では呼ばれない想定 —
    呼ばれたら §4.2-4 の evaluable ゲートが壊れている。**[profitability-
    floor] T1 Step 1-2**: `pf`/`avg_r` の既定はフロアを通す値
    (`_check_profitability_floor` の直接インデックス対応)。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        record_fn(_fake_record_kwargs(settings=settings, trades=40,
                                      scope="holdout_gate", **kwargs))
        return {"trades": 40, "pf": pf, "avg_r": avg_r, "evaluable": True}
    return _run


def test_approval_payload_analysis_ids_come_from_ledger_not_agent_claim(
        improve_env, monkeypatch):
    """agent が selection_rationale に虚偽の analysis id / 回数を書いても、
    payload の analysis_run_ids / trial_count / analysis_call_count は
    RPC 台帳の実測値だけから作られる (§4.2-5, §8.1-16)。"""
    app, root = improve_env
    conn = app.conn_core
    output = _plugin_artifact("ledger_pin_e2e")
    output["selection_rationale"] = (
        "used analysis_run_ids=[9999,9998] trial_count=100000")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "ledger_pin_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
        _write_staging_plugin(
            ctx.staging_dir, "ledger_probe_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        monkeypatch.setattr(
            "agentic_fx.loops.improve_loop.holdout.run_in_sample",
            _fake_in_sample_metrics(40))
        args = {"name": "ledger_probe_e2e", "pair": "USDJPY"}
        assert worker.kwargs["on_rpc_begin"]("run_backtest") is True
        outcome = worker.kwargs["rpc_handlers"]["run_backtest"](args)
        worker.kwargs["on_rpc_accepted"]("run_backtest", args, outcome)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    payload_json = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    payload = json.loads(payload_json)
    # 逐語乖離の申告 (着手前検証): 旧稿は payload の JSON 文字列全体に
    # "9999"/"100000" が含まれないことを assert していたが、設計書
    # §4.2-5 は `selection_rationale: <agent>` を**そのまま**保存すると
    # 明記している — 本テストが agent 出力として与えた
    # `selection_rationale` 自体に "9999"/"100000" の文字列を含むため、
    # 文字列全体を見る assert は「保護されているべきでないフィールド」
    # まで検査してしまい、実装が正しくてもほぼ確実に落ちる (実際に
    # 落ちた)。台帳由来であるべきフィールド (`analysis_run_ids`/
    # `trial_count`) だけを個別に検査する形に直す。
    assert payload["analysis_run_ids"] == []
    assert payload["trial_count"] == 1
    assert payload["analysis_call_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE mission_id=?",
        (ctx.mission_id,)).fetchone()[0] == 1
    assert payload["selection_rationale"] == (
        "used analysis_run_ids=[9999,9998] trial_count=100000")


def test_strategy_below_evaluable_min_trades_becomes_observation(improve_env):
    """§4.2-4: strategy artifact の合計取引数が `EVALUABLE_MIN_TRADES` (=30)
    未満なら承認申請を出さず observation に落ちる (`insufficient_trades:<n>`)。
    `run_holdout_gate` は呼ばれない (評価不能で holdout に進まないこと自体が
    このテストの killer)。

    逐語乖離の申告 (着手前検証): 旧稿は `_plugin_artifact(..., kind=
    "strategy")` で agent の申告を strategy にしていたが、実際に staging
    へ書く候補は `_PASSING_INDICATOR_CONFIG` (`kind: indicator`) のまま
    だった。strategy 採用ゲートの発火可否は `_read_candidate_kind` が
    config.yaml の実体を読んで決める (agent の申告は信用しない、§4.2-1)
    ため、この乖離により strategy ゲート自体が一度も発火せず素通りして
    いた (approval_count が 0 でなく 1 になっていた)。`_PASSING_STRATEGY_*`
    (`evaluate(df, indicators, signals, params)` を実装、`kind: strategy`)
    に差し替える。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("low_trades_strategy_e2e", kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    holdout_calls: list[bool] = []

    def _holdout_should_not_be_called(*a, **kw):
        holdout_calls.append(True)
        raise AssertionError("run_holdout_gate must not be called when "
                             "trades < EVALUABLE_MIN_TRADES")

    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_metrics(29)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _holdout_should_not_be_called):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "low_trades_strategy_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    assert holdout_calls == []
    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 0

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1].startswith("insufficient_trades:")
    persisted = conn.execute(
        "SELECT mission_id, scope FROM backtest_runs WHERE mission_id=?",
        (ctx.mission_id,),
    ).fetchall()
    assert [(row[0], row[1]) for row in persisted] == [
        (ctx.mission_id, "in_sample")
    ]


def test_artifact_name_traversal_fails_mission(improve_env):
    """§4.2-1: `artifact.name` が正規形 (`^[a-z][a-z0-9_]{0,63}$`) でない
    (`../` を含む) 候補は出力検査で不合格になり、Mission は `failed`。
    Tx-1 (backlog 追記・選択) には一切進まない (敗者経路とも異なり、遷移
    そのものが起きない) — `source != 'system'` の行は 0 件のまま。T4
    変更点2 が `_finalize_output_invalid` に system note (source='system',
    status='note') を新設したため、そちらは別途 1 件だけ増える。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("../../docs/examples/plugins/rsi_indicator")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        # staging には正規形の名前で書く (worker はここへ書いたつもりでも、
        # 検査対象は agent が申告した `artifact.name` の文字列そのもの)。
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    mission_row = conn.execute(
        "SELECT status FROM missions WHERE id=?", (ctx.mission_id,)
    ).fetchone()
    assert mission_row[0] == "failed"

    backlog_count = conn.execute(
        "SELECT COUNT(*) FROM improvement_backlog WHERE source != 'system'"
    ).fetchone()[0]
    assert backlog_count == 0

    note = conn.execute(
        "SELECT idea, last_result FROM improvement_backlog "
        "WHERE source='system' AND status='note'").fetchone()
    assert note is not None
    assert "schema" in note["idea"]
    assert f"mission #{ctx.mission_id}" in note["last_result"]

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count == 0

    run_row = conn.execute(
        "SELECT result, finished_at FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()
    assert run_row[0] is None
    assert run_row[1] is not None

    assert not (root / "plugins" / "_staging" / str(ctx.mission_id)).exists()


def test_report_tmp_symlink_fails_closed(improve_env):
    """§4.2-6: `reports/.tmp/improve-<mission_id>.md.part` へ事前に symlink を
    置くと、`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` での書込みが fail closed に
    なる (symlink を辿って任意ファイルへ書かない)。§4.2-7 のとおり
    `result=NULL` + `report_state='failed'`、backlog は
    `observation(report_failed:...)`。**この時点では `report_path` に
    NULL を要求しない** (それは公開 (rename) 失敗の補償 tx の話 — ⑧ 参照)。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("bad_gate_symlink_e2e")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "bad_gate_symlink_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)  # ゲート不合格 → レポート経路へ入る
        # 逐語乖離の申告 (着手前検証): outbox の実パスは
        # `data/improve_reports` (`test_gate_failure_stops_at_report_
        # no_approval_request` のコメント参照、`root/"reports"` ではない)。
        tmp_dir = root / "data" / "improve_reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        evil_target = tmp_dir / "evil-target.md"
        evil_target.write_text("should never be reached", encoding="utf-8")
        (tmp_dir / f"improve-{ctx.mission_id}.md.part").symlink_to(
            evil_target)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    assert evil_target.read_text(encoding="utf-8") == \
        "should never be reached"

    run_row = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()
    assert run_row[0] is None
    assert run_row[1] == "failed"

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1].startswith("report_failed:")


# プラン10 Task12 Step3: `_finalize_loser` の OSError 未捕捉是正 pin
def test_finalize_loser_report_tmp_symlink_fails_closed(improve_env):
    """§4.2 手順2 敗者経路 (`_finalize_loser`) も `_finalize_gate_failed`
    と同じ扱いに揃える — 敗者用の自動レポート `.tmp/*.part` へ事前に
    symlink を置くと `_write_report_part` が OSError を投げるが、旧実装は
    捕まえずに突き抜けて `commit()` が非終端のまま落ちていた。ここでは
    2 度目の `_select_and_bind` の CAS を確実に負けさせるため、backlog を
    先に 'selected' へ固定してから (`backlog_store.select_for_mission` で
    1 回目の CAS を先取り消費する) 同じ backlog id を選ぶ 2 本目の
    mission を流す — スレッド競合を作らずとも `won=False` を決定的に
    再現できる (`test_concurrent_duplicate_selection_loser_becomes_
    observation` は「実際の並行性」の検証が主眼、本 pin は OSError 分岐
    だけを狙うためあえて逐次呼び出しにする)。backlog は敗者経路では
    一切遷移しない (`_finalize_loser` の docstring 参照) ため、既に
    'selected' のままであることも確認する。"""
    app, root = improve_env
    conn = app.conn_core

    backlog_id = backlog_store.add(conn, "loser symlink idea", "user", NOW)
    won = backlog_store.select_for_mission(conn, backlog_id, now=NOW)
    assert won is True  # 先取りして CAS を消費済みにする

    output = _plugin_artifact("loser_symlink_e2e")
    output["selected"] = {"backlog_id": backlog_id, "idea": "loser symlink idea"}
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "loser_symlink_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
        tmp_dir = root / "data" / "improve_reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        evil_target = tmp_dir / "evil-loser-target.md"
        evil_target.write_text("should never be reached", encoding="utf-8")
        (tmp_dir / f"improve-{ctx.mission_id}.md.part").symlink_to(
            evil_target)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    assert evil_target.read_text(encoding="utf-8") == \
        "should never be reached"

    run_row = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()
    assert run_row[0] is None
    assert run_row[1] == "failed"

    backlog_row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog_row[0] == "selected"  # 敗者経路は backlog に触れない


# プラン10 Task12 Step3: `_prepare_report_if_applicable` の OSError
# 未捕捉是正 pin
def test_prepare_report_if_applicable_symlink_fails_closed(improve_env):
    """§3.5/§4.2 手順6 (`artifact.type=='report'`) も `_finalize_gate_failed`
    と同じ扱いに揃える — 承認申請を出さない `report` artifact 経路
    (`proposal_kind != 'risk_gate'`) で `.tmp/*.part` へ事前に symlink を
    置くと `_write_report_part` が OSError を投げるが、旧実装は捕まえずに
    突き抜けて `commit()` が非終端のまま落ちていた。"""
    app, root = improve_env
    conn = app.conn_core

    output = _report_artifact("core", title="prepare report symlink")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        tmp_dir = root / "data" / "improve_reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        evil_target = tmp_dir / "evil-prepare-target.md"
        evil_target.write_text("should never be reached", encoding="utf-8")
        (tmp_dir / f"improve-{ctx.mission_id}.md.part").symlink_to(
            evil_target)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    assert evil_target.read_text(encoding="utf-8") == \
        "should never be reached"

    run_row = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()
    assert run_row[0] is None
    assert run_row[1] == "failed"

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1].startswith("report_failed:")


# precheck 2026-08-22 wave2: T12-B5 T12-M3
def test_timeout_mission_leaves_no_backtest_or_analysis_run_rows(
        improve_env):
    """§3.6: timeout した Mission は `finish_improve_mission` の 1 tx で
    終端され、台帳は `DISCARDED` (`backtest_runs`/`analysis_runs` に行が
    残らない)。台帳を実際に使わせてから timeout させる — 未使用の台帳に
    対する不在確認は空洞テストになるため、`rpc_handlers` 経由で
    `run_backtest`/`analyze_corr` を最低 1 回ずつ呼んでから timeout を
    返す。**`backtest_runs.mission_id`/`analysis_runs.mission_id` 列は
    Task 10 が追加する** (第 2 波裁定「Task 12 (report-task12.md B-1〜B-17)」
    節: 列そのものは Task 10 の 8-C 相当 Step が `_ensure_column` で追加する
    — 本ファイルの SQL はその追加に依存する。Task 10 完了前にこの Step だけを
    先行実行すると `sqlite3.OperationalError: no such column: mission_id` に
    なる)。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("timeout_e2e")
    result = MissionResult(status="timeout", output=None, transcript=[],
                          reason="mission timeout (fake)")

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        # `run_backtest_handler` は `plugin_loader._discover_one` を
        # try/except の外で呼ぶため、候補ディレクトリ自体が存在しないと
        # (`FileNotFoundError`) ここで直接クラッシュする — 台帳記録に
        # 到達させるため実在する strategy 候補を置く (中身の合否は
        # 本テストの関心外、`_discover_one` が発見できればよい)。
        _write_staging_plugin(
            ctx.staging_dir, "ledger_probe_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        # `run_backtest` は staging に候補が無いため内部で失敗するが、
        # handler 自身が例外を飲んで `{"error": "backtest_failed"}` を
        # 返す設計 (`_build_rpc_handlers.run_backtest_handler`) なので
        # tool 関数は正常終了し `ledger.record` まで到達する。
        bt_args = {"name": "ledger_probe_e2e", "pair": "USDJPY"}
        assert worker.kwargs["on_rpc_begin"]("run_backtest") is True
        bt_outcome = worker.kwargs["rpc_handlers"]["run_backtest"](bt_args)
        worker.kwargs["on_rpc_accepted"]("run_backtest", bt_args, bt_outcome)
        corr_args = {"request": {"pairs": ["USDJPY"]}}
        assert worker.kwargs["on_rpc_begin"]("analyze_corr") is True
        corr_outcome = worker.kwargs["rpc_handlers"]["analyze_corr"](corr_args)
        worker.kwargs["on_rpc_accepted"]("analyze_corr", corr_args, corr_outcome)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    # M-3 (report-task12.md): except: pass のせいで台帳に 1 件も積まれない
    # まま「0 行」が別の理由 (呼出し自体が失敗) で通ってしまう空洞化を防ぐ —
    # `ImproveRpcLedger.entries()` は freeze() 後 (= commit 相突入後) のみ
    # 呼べる (骨格 Interfaces) ため、ここ (commit 完了後) で読む。
    assert len(ctx.ledger.entries()) >= 1

    backtest_rows = conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE mission_id=?",
        (ctx.mission_id,)).fetchone()[0]
    assert backtest_rows == 0
    analysis_rows = conn.execute(
        "SELECT COUNT(*) FROM analysis_runs WHERE mission_id=?",
        (ctx.mission_id,)).fetchone()[0]
    assert analysis_rows == 0

    run_row = conn.execute(
        "SELECT result, finished_at FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()
    assert run_row[0] is None
    assert run_row[1] is not None


def test_report_creation_failure_leaves_result_null(improve_env):
    """§4.2-7: レポートの一時ファイル作成そのものが失敗した (`reports/.tmp`
    が書込不可) とき、`improvement_runs.result IS NULL` かつ
    `report_state='failed'`。symlink 事前設置 (③) とは別の失敗経路
    (パーミッション) をここで踏む。"""
    if os.geteuid() == 0:
        pytest.skip("root では chmod による書込拒否を再現できない")
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("report_write_fails_e2e")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    # 逐語乖離の申告 (着手前検証): outbox の実パスは `data/improve_reports`
    # (`test_gate_failure_stops_at_report_no_approval_request` のコメント
    # 参照、`root/"reports"` ではない)。
    tmp_dir = root / "data" / "improve_reports" / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.chmod(0o500)  # 書込不可 (自分の書込みビットだけ落とす)
    try:
        with patch("agentic_fx.runners.worker_runner.WorkerRunner",
                   lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
            mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
            _write_staging_plugin(
                ctx.staging_dir, "report_write_fails_e2e",
                _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
                _FAILING_INDICATOR_TEST)
            mission_result = worker.run(mission)
            loop.commit(mission=mission, ctx=ctx, result=mission_result,
                       now=NOW)
    finally:
        tmp_dir.chmod(0o700)

    run_row = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()
    assert run_row[0] is None
    assert run_row[1] == "failed"


def test_risk_gate_proposal_becomes_unsupported_observation(improve_env):
    """§3.5/§4.2-6/R12-(b): `proposal_kind=risk_gate` の report は本プランで
    受理せず observation `unsupported_in_plan10` に落ち、report ファイルは
    一切書かれない。"""
    app, root = improve_env
    conn = app.conn_core

    output = _report_artifact("risk_gate", title="widen SL on USDJPY")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1] == "unsupported_in_plan10:risk_gate"

    report_files = list((root / "reports").glob("improve-*.md"))
    assert report_files == []


# precheck 2026-08-22 wave2: T12-B5
def test_strategy_baseline_falls_back_to_no_strategy_row(improve_env):
    """§4.2-4: 合計取引数 ≥30 (評価可能) かつ同名の live/D4-approved strategy
    が存在しないとき、baseline は `variant='no_strategy'` の明示行になる
    (null にしない)。`run_holdout_gate` の fake も呼び出し、holdout 側にも
    baseline 行が要ることを確認する必要はここでは問わない (in-sample の
    baseline 行だけを pin する — holdout 側は別途骨格実装時に追加検討)。
    `backtest_runs.mission_id` 列は Task 10 が追加する (本ファイル冒頭の
    `test_timeout_mission_leaves_no_backtest_or_analysis_run_rows` の
    docstring 参照)。

    逐語乖離の申告 (着手前検証): `test_strategy_below_evaluable_min_
    trades_becomes_observation` と同じ乖離 — 実際に staging へ書く候補が
    `_PASSING_INDICATOR_CONFIG` (`kind: indicator`) のままで strategy
    ゲートが発火しなかった。`_PASSING_STRATEGY_*` に差し替える。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("no_baseline_strategy_e2e", kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_metrics(40)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_metrics()):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "no_baseline_strategy_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    baseline_row = conn.execute(
        "SELECT variant, plugin_ref, content_hash, kind FROM backtest_runs "
        "WHERE mission_id=? AND variant='no_strategy'",
        (ctx.mission_id,)).fetchone()
    assert baseline_row is not None
    variant, plugin_ref, content_hash, kind = baseline_row
    assert plugin_ref == "no_strategy:no_baseline_strategy_e2e"
    assert content_hash is not None
    assert kind == "strategy"

    null_baseline = conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE mission_id=? "
        "AND variant='baseline' AND ref_plugin_ref IS NULL "
        "AND plugin_ref IS NULL", (ctx.mission_id,)).fetchone()[0]
    assert null_baseline == 0


def test_strategy_profitability_floor_reroutes_to_gate_failed_unprofitable(
        improve_env):
    """[profitability-floor] T1 Step 1-3 (2026-09-12): in_sample 段が
    収益性フロア不合格のとき、承認申請を出さず `_finalize_gate_failed` の
    フロア経路 (`reason="unprofitable"`, `mission_outcome="unprofitable"`)
    に再ルートする — `last_result` は固定文言 `unprofitable`、
    `backtest_runs.mission_outcome` も `unprofitable`、holdout は回らない
    (`run_holdout_gate` 未呼び出し)、レポート本文にフロア詳細が入る。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("floor_reroute_e2e", kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )
    holdout_calls = []
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               lambda *a, **kw: (
                   holdout_calls.append(1),
                   _fake_holdout_metrics()(*a, **kw))[1]):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "floor_reroute_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    assert holdout_calls == []  # enforce: holdout を回さず即終端

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1] == "unprofitable"  # 固定文言・完全一致

    run_row = conn.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE mission_id=? "
        "AND scope='in_sample'", (ctx.mission_id,)).fetchone()
    assert run_row is not None
    assert run_row[0] == "unprofitable"

    pending = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='pending'"
    ).fetchone()[0]
    assert pending == 0  # 承認申請は出さない

    report_files = list((root / "data" / "improve_reports").glob(
        f"improve-*-{ctx.mission_id}.md"))
    assert len(report_files) == 1
    report_text = report_files[0].read_text(encoding="utf-8")
    assert "Profitability floor detail" in report_text
    assert "pf=" in report_text  # 全数値は親専有レポートにのみ現れる
    # F4-5 (archive INDEX の status=='unprofitable') はこの最小 e2e
    # フィクスチャでは検証できない —
    # `_write_archive_index_safe` は `candidate_archives` に行が 0 件の
    # mission には何も書かない契約 (`list_by_mission` が空)。本フィクス
    # チャは `run_backtest` RPC 経由のアーカイブ生成を経ないため、この
    # pin は `_settle_ledger_after_commit` の branch-local outcome 単体
    # pin (下記 F4-9 相当、mission_outcome の一致) で代替する。

    # [profitability-floor-fix] G2: 落ちた段 (in_sample) + 落ちた pair
    # (USDJPY) + 8 指標の表 (in_sample のみ — holdout は回っていない)
    # + 適用閾値がレポート本文に出る。
    assert "failed_stage: in_sample" in report_text
    assert "failed_pairs: USDJPY" in report_text
    g = app.settings.improve.gate
    assert (f"thresholds: min_pf={g.min_pf} "
            f"require_positive_avg_r={g.require_positive_avg_r} "
            f"require_holdout_evaluable={g.require_holdout_evaluable}"
            ) in report_text
    assert "### in_sample" in report_text
    assert "### holdout" not in report_text  # in_sample 段落ち → holdout 表無し
    assert "| USDJPY | 40 | 0.3 |" in report_text  # trades/pf 列

    # [profitability-floor-fix] G1: activity の `gate_failed` 行に、
    # `reason=unprofitable` と同じ行で適用閾値 3 値が出る。
    activity_text = (root / "logs" / "activity.log").read_text()
    gate_failed_lines = [
        line for line in activity_text.splitlines()
        if "gate_failed" in line and f"mission={ctx.mission_id}" in line]
    assert len(gate_failed_lines) == 1
    assert "reason=unprofitable" in gate_failed_lines[0]
    assert (f"min_pf={g.min_pf} require_positive_avg_r={g.require_positive_avg_r} "
            f"require_holdout_evaluable={g.require_holdout_evaluable}"
            ) in gate_failed_lines[0]

    # [profitability-floor-fix] codex 差分レビュー Important (2026-09-13):
    # `gate_failed` activity 行**全体**には `report_detail` 由来の内容
    # (段名・pair 名・生成績値・failed_stage/failed_pairs) が一切出ない
    # — 閾値そのもの (`min_pf=`/`require_positive_avg_r=`/
    # `require_holdout_evaluable=`、`floor_settings_kv` 由来) だけが例外。
    # 逆変異 = `_finalize_gate_failed` の activity 文字列に
    # `report_detail` を連結する → RED。
    line = gate_failed_lines[0]
    assert not re.search(r"(?<!require_)holdout", line)  # require_holdout_evaluable= は許容
    assert "in_sample" not in line
    assert "USDJPY" not in line  # pair 名
    assert "trades" not in line
    assert not re.search(r"(?<!min_)pf=", line)  # 閾値の min_pf= だけ許容
    assert not re.search(r"(?<!require_positive_)avg_r", line)
    assert "failed_stage" not in line
    assert "failed_pairs" not in line
    assert "0.3" not in line  # レポート固有値 canary (in_sample pf)
    assert "-0.1" not in line  # レポート固有値 canary (in_sample avg_r)


def test_f2_6_f4_series_holdout_only_failure_marks_all_gate_rows_unprofitable(
        improve_env):
    """F2-6/F4-1/F4-2/F4-3/F4-6 (2026-09-13、F 番号 gap 充足): in_sample
    段は合格 (pf=1.5/avg_r=+0.2) だが holdout 段が不合格 (pf=0.8) の
    候補は、in_sample 行**と** holdout_gate 行の全 pair が
    `mission_outcome='unprofitable'` になる (F2-6)。副作用は F1 系と同じ:
    backlog は `observation` かつ `list_open` に含まれる (F4-2)、
    approval_requests に行が増えない (F4-3)、staging が削除される
    (F4-6)。`mission_outcome` は `None` にならない (F4-1)。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("holdout_floor_e2e", kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_metrics(40, pf=1.5, avg_r=0.2)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_metrics(pf=0.8, avg_r=0.2)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "holdout_floor_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    backlog_row = conn.execute(
        "SELECT id, status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row["status"] == "observation"
    assert backlog_row["last_result"] == "unprofitable"

    # F4-2: observation は list_open に残る (R8、次回再挑戦できる)。
    from agentic_fx.store import backlog as backlog_store
    open_ids = {row["id"] for row in backlog_store.list_open(conn)}
    assert backlog_row["id"] in open_ids

    # F2-6/F4-1: in_sample 行と holdout_gate 行の全 pair が unprofitable
    # (None にならない)。
    rows = conn.execute(
        "SELECT scope, mission_outcome FROM backtest_runs "
        "WHERE mission_id=?", (ctx.mission_id,)).fetchall()
    scopes_seen = {r["scope"] for r in rows}
    assert scopes_seen == {"in_sample", "holdout_gate"}
    assert all(r["mission_outcome"] == "unprofitable" for r in rows)

    # F4-3: approval_requests に行が増えない。
    pending = conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0]
    assert pending == 0

    # F4-6: staging が削除される。
    assert not ctx.staging_dir.exists()

    # [profitability-floor-fix] G2: holdout 段で落ちた場合は in_sample・
    # holdout 両方の表がレポート本文に出る (in_sample は合格しているので
    # failed_stage/failed_pairs には出ない)。
    report_files = list((root / "data" / "improve_reports").glob(
        f"improve-*-{ctx.mission_id}.md"))
    assert len(report_files) == 1
    report_text = report_files[0].read_text(encoding="utf-8")
    assert "failed_stage: holdout" in report_text
    assert "failed_pairs: USDJPY" in report_text
    assert "### in_sample" in report_text
    assert "### holdout" in report_text
    assert "| USDJPY | 40 | 1.5 |" in report_text  # in_sample 行
    assert "| USDJPY | 40 | 0.8 |" in report_text  # holdout 行

    # [profitability-floor-fix] G1: activity の gate_failed 行にも同じ
    # 閾値 3 値が出る。
    g = app.settings.improve.gate
    activity_text = (root / "logs" / "activity.log").read_text()
    gate_failed_lines = [
        line for line in activity_text.splitlines()
        if "gate_failed" in line and f"mission={ctx.mission_id}" in line]
    assert len(gate_failed_lines) == 1
    assert (f"min_pf={g.min_pf} require_positive_avg_r={g.require_positive_avg_r} "
            f"require_holdout_evaluable={g.require_holdout_evaluable}"
            ) in gate_failed_lines[0]

    # [profitability-floor-fix] codex 差分レビュー Important (2026-09-13):
    # holdout 段落ちのときも `gate_failed` activity 行**全体**には
    # `report_detail` 由来の内容が一切出ない — 閾値の `min_pf=` (と
    # `require_positive_avg_r=`) だけが例外。逆変異 = `_finalize_gate_
    # failed` の activity 文字列に `report_detail` を連結する → RED。
    line = gate_failed_lines[0]
    assert not re.search(r"(?<!require_)holdout", line)  # require_holdout_evaluable= は許容
    assert "in_sample" not in line
    assert "USDJPY" not in line  # pair 名
    assert "trades" not in line
    assert not re.search(r"(?<!min_)pf=", line)  # 閾値の min_pf= だけ許容
    assert not re.search(r"(?<!require_positive_)avg_r", line)
    assert "failed_stage" not in line
    assert "failed_pairs" not in line
    assert "1.5" not in line  # レポート固有値 canary (in_sample pf)
    assert "0.8" not in line  # レポート固有値 canary (holdout pf)


_TWO_PAIR_STRATEGY_CONFIG = """
kind: strategy
pairs: [USDJPY, EURUSD]
timeframe: 1h
exit_mode: levels
params: {}
"""

# [profitability-floor-fix] codex 差分レビュー Important (2026-09-13):
# 2 pair とも 8 指標が全部異なる値にし、列 1 つ落とす変異や 2 番目の pair
# を落とす変異のどちらも「全 8 値 × 2 pair」の厳密一致で確実に KILL する。
_TWO_PAIR_IN_SAMPLE_METRICS = {
    "USDJPY": {"trades": 40, "pf": 1.5, "win_rate": 0.5, "avg_r": 0.3,
               "max_drawdown": 0.05, "total_pnl": 120.0,
               "kill_switch_latches": 0, "evaluable": True},
    "EURUSD": {"trades": 45, "pf": 1.8, "win_rate": 0.55, "avg_r": 0.25,
               "max_drawdown": 0.04, "total_pnl": 200.0,
               "kill_switch_latches": 2, "evaluable": True},
}
_TWO_PAIR_HOLDOUT_METRICS = {
    "USDJPY": {"trades": 35, "pf": 0.7, "win_rate": 0.4, "avg_r": -0.2,
               "max_drawdown": 0.08, "total_pnl": -50.0,
               "kill_switch_latches": 1, "evaluable": True},
    "EURUSD": {"trades": 38, "pf": 1.2, "win_rate": 0.6, "avg_r": 0.15,
               "max_drawdown": 0.03, "total_pnl": 80.0,
               "kill_switch_latches": 3, "evaluable": True},
}


def _fake_in_sample_per_pair(metrics_by_pair: dict):
    """`run_in_sample` の fake — pair (`symbol` kwarg) ごとに異なる
    `metrics_by_pair[symbol]` を返す (単一値を全 pair で共有する既存の
    `_fake_in_sample_with_metrics` と違い、2 pair 全部異なる値の fixture
    を作れる)。"""
    def _run(settings, *, history_conn, record_fn, symbol, **kwargs):
        m = metrics_by_pair[symbol]
        record_fn(_fake_record_kwargs(settings=settings, trades=m["trades"],
                                      scope="in_sample", pair=symbol, **kwargs))
        return dict(m)
    return _run


def _fake_holdout_per_pair(metrics_by_pair: dict):
    """`run_holdout_gate` の fake — pair ごとに異なる metrics を返す。"""
    def _run(settings, *, history_conn, record_fn, symbol, **kwargs):
        m = metrics_by_pair[symbol]
        record_fn(_fake_record_kwargs(settings=settings, trades=m["trades"],
                                      scope="holdout_gate", pair=symbol, **kwargs))
        return dict(m)
    return _run


def test_floor_report_detail_holdout_fail_two_pairs_full_metrics(improve_env):
    """[profitability-floor-fix] codex 差分レビュー Important (2026-09-13):
    2 pair (値がすべて異なる fixture) で holdout 段が不合格になったとき、
    レポート本文のヘッダ行 (8 列名) が完全一致し、in_sample・holdout の
    各表に**全 8 値 × 2 pair** がそのまま出ることを固定する。列を 1 つ
    落とす変異 (代表: `win_rate`/`kill_switch_latches`) や 2 番目の pair
    を落とす変異のどちらも、行全体の厳密一致で KILL される。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("floor_two_pair_e2e", kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_per_pair(_TWO_PAIR_IN_SAMPLE_METRICS)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_per_pair(_TWO_PAIR_HOLDOUT_METRICS)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "floor_two_pair_e2e",
            _PASSING_STRATEGY_PY, _TWO_PAIR_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert backlog_row[1] == "unprofitable"

    report_files = list((root / "data" / "improve_reports").glob(
        f"improve-*-{ctx.mission_id}.md"))
    assert len(report_files) == 1
    report_text = report_files[0].read_text(encoding="utf-8")

    header = ("| pair | trades | pf | win_rate | avg_r | max_drawdown | "
              "total_pnl | kill_switch_latches | evaluable |")
    assert header in report_text

    assert "### in_sample" in report_text
    assert "### holdout" in report_text
    assert ("| USDJPY | 40 | 1.5 | 0.5 | 0.3 | 0.05 | 120.0 | 0 | True |"
            in report_text)
    assert ("| EURUSD | 45 | 1.8 | 0.55 | 0.25 | 0.04 | 200.0 | 2 | True |"
            in report_text)
    assert ("| USDJPY | 35 | 0.7 | 0.4 | -0.2 | 0.08 | -50.0 | 1 | True |"
            in report_text)
    assert ("| EURUSD | 38 | 1.2 | 0.6 | 0.15 | 0.03 | 80.0 | 3 | True |"
            in report_text)


_IN_SAMPLE_METRICS_FOR_PAYLOAD = {
    "trades": 40, "pf": 1.474, "win_rate": 0.487, "avg_r": 0.188,
    "max_drawdown": 0.0373, "total_pnl": 100.0}
_HOLDOUT_METRICS_FOR_PAYLOAD = {
    "trades": 54, "pf": 0.904, "win_rate": 0.3, "avg_r": -0.037,
    "max_drawdown": 0.0392, "total_pnl": -10.0}


def _fake_in_sample_with_metrics(metrics: dict):
    """`run_in_sample` の fake。`record_fn` へ save_kwargs 形の in_sample 行を
    積み、`per_pair[pair]` (= `StrategyGateVerdict.candidate_metrics`) には
    `metrics` そのものを返す — [approval-payload-missing-gate-metrics]
    是正のテストは in-sample と holdout で異なる数値を使い、コピー/スワップ
    系の変異 (holdout に in_sample の値を代入する等) を殺す。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        record_fn(_fake_record_kwargs(settings=settings, trades=metrics["trades"],
                                      scope="in_sample", **kwargs))
        return dict(metrics)
    return _run


def _fake_holdout_with_metrics(metrics: dict):
    """`run_holdout_gate` の fake。`record_fn` へ渡す save_kwargs の
    `metrics` を `_fake_record_kwargs` の既定 (`{"trades": trades}` のみ) から
    差し替え、pf/avg_r/max_drawdown まで持つ現物形にする。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        row = _fake_record_kwargs(settings=settings, trades=metrics["trades"],
                                  scope="holdout_gate", **kwargs)
        row["metrics"] = dict(metrics)
        record_fn(row)
        return dict(metrics)
    return _run


def test_approval_payload_includes_in_sample_and_holdout_metrics(improve_env):
    """[approval-payload-missing-gate-metrics] (A4 10 回目 claude #69 観測 A、
    2026-09-11): strategy candidate の承認 payload には、ゲートが実際に測った
    in-sample / holdout の成績が載らなければならない (それまでは
    `gate_metrics` に代入されるのが `baseline` だけで、両方とも構造的に
    None だった — holdout で負けている候補でも人間の承認材料には自己申告
    しか出てこない、という欠陥)。single pair (USDJPY 1 本) の候補なので
    `payload["holdout"]` はブリーフの取り決め通り pair→metrics の dict では
    なく metrics dict そのもの。`payload["in_sample"]` は
    `StrategyGateVerdict.candidate_metrics` をそのまま (pair→metrics の
    dict) 載せる — この非対称はブリーフの明示指定どおり。"""
    app, root = improve_env
    conn = app.conn_core

    output = _plugin_artifact("payload_metrics_e2e", kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,
    )
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_with_metrics(_IN_SAMPLE_METRICS_FOR_PAYLOAD)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_with_metrics(_HOLDOUT_METRICS_FOR_PAYLOAD)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, "payload_metrics_e2e",
            _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    payload_json = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin'"
        ).fetchone()[0]
    payload = json.loads(payload_json)

    assert payload["in_sample"] == {"USDJPY": _IN_SAMPLE_METRICS_FOR_PAYLOAD}
    assert payload["holdout"] == _HOLDOUT_METRICS_FOR_PAYLOAD
    assert payload["eval_timeframe"] == "1h"


def test_report_outbox_state_transitions_published_then_rename_failure(
        improve_env):
    """§4.1「report の公開状態」: 1 本目は `.tmp/*.part` → Tx-2
    (`report_state=prepared`) → COMMIT 後に rename 公開 →
    `published` の正常系。2 本目は最終ファイル名を先に衝突させて
    `RENAME_NOREPLACE` を失敗させ、公開失敗の補償 tx
    (`report_state='failed'`, `result=NULL`, `report_path=NULL`,
    backlog `done→observation(report_failed:...)`) を確認する。"""
    app, root = improve_env
    conn = app.conn_core

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )

    # --- 1 本目: 正常系 (ゲート不合格 → レポートのみ経路で published まで) ---
    output1 = _plugin_artifact("outbox_ok_e2e")
    result1 = MissionResult(status="completed", output=output1, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result1, **kw)):
        mission1, ctx1, worker1 = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx1.staging_dir, "outbox_ok_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)
        mission_result1 = worker1.run(mission1)
        loop.commit(mission=mission1, ctx=ctx1, result=mission_result1,
                   now=NOW)

    run1 = conn.execute(
        "SELECT report_state, report_path FROM improvement_runs WHERE id=?",
        (ctx1.run_id,)).fetchone()
    assert run1[0] == "published"
    final_path1 = root / run1[1]
    assert final_path1.exists()
    # F-3 是正 (検収 task12 2026-08-27): outbox 規約は
    # `data/improve_reports/improve-YYYY-MM-DD-<mission_id>.md`
    # (ディレクトリは 3 箇所の既存実装・プラン本文 L20140 と一致、
    # 最終名のみ日付を追加 — 設計書 §4.2 逐語どおり)。
    assert final_path1.name == f"improve-{NOW:%Y-%m-%d}-{ctx1.mission_id}.md"
    assert not (root / "data" / "improve_reports" / ".tmp" /
               f"improve-{ctx1.mission_id}.md.part").exists()

    # --- 2 本目: 最終名を先取りして rename 公開を失敗させる ---
    output2 = _plugin_artifact("outbox_fail_e2e")
    result2 = MissionResult(status="completed", output=output2, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result2, **kw)):
        mission2, ctx2, worker2 = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx2.staging_dir, "outbox_fail_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)
        expected_final = (root / "data" / "improve_reports" /
                          f"improve-{NOW:%Y-%m-%d}-{ctx2.mission_id}.md")
        expected_final.parent.mkdir(parents=True, exist_ok=True)
        expected_final.write_text("pre-existing, blocks RENAME_NOREPLACE",
                                  encoding="utf-8")
        mission_result2 = worker2.run(mission2)
        loop.commit(mission=mission2, ctx=ctx2, result=mission_result2,
                   now=NOW)

    run2 = conn.execute(
        "SELECT result, report_state, report_path FROM improvement_runs "
        "WHERE id=?", (ctx2.run_id,)).fetchone()
    assert run2[0] is None
    assert run2[1] == "failed"
    assert run2[2] is None

    backlog2 = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "WHERE id=(SELECT backlog_id FROM improvement_runs WHERE id=?)",
        (ctx2.run_id,)).fetchone()
    assert backlog2[0] == "observation"
    assert backlog2[1].startswith("report_failed:")


@pytest.mark.skipif(os.geteuid() == 0, reason="root は 0500 dir でも書ける")
def test_source_snapshot_dir_is_readonly_to_parent_after_prepare(
        improve_env):
    """§4 prepare: `source_snapshot_dir` は 0500 で作られる。worker 側
    (実サブプロセス) の `plugins/`/`docs/` EACCES 半分は
    `tests/integration/test_improve_forbidden_regression.py` (Task 7、
    実プロセス、レビュー1周目 I2 で正式ファイルへ一本化) が
    担う — 本テストは FakeImproveWorkerRunner がサブプロセスを起こさない
    ため Landlock を経由しない、**親プロセス自身**が `source_snapshot_dir`
    へ書き込もうとしても `PermissionError` になることだけを pin する
    (パーミッションビットの pin。Landlock の pin ではない)。"""
    app, root = improve_env

    loop = ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag,  # wave2-recheck: T10-B10
    )
    result = MissionResult(status="completed",
                          output=_plugin_artifact("snapshot_ro_e2e"),
                          transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        try:
            assert (ctx.source_snapshot_dir.stat().st_mode & 0o777) == 0o500
            with pytest.raises(PermissionError):
                (ctx.source_snapshot_dir / "should_not_be_writable.txt"
                 ).write_text("nope", encoding="utf-8")
        finally:
            # commit 相を通して mission/run を正しく終端させる (後始末)。
            mission_result = worker.run(mission)
            loop.commit(mission=mission, ctx=ctx, result=mission_result,
                       now=NOW)


def test_run_backtest_handler_refuses_unresolved_dependency(tmp_path):
    """F4: 親は backtest を走らせず `{"started": false, ...}` を返す。
    staging 内 indicator は `available` に出ない。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    # staging に indicator 候補を置いても inventory には入らない
    fx.write_indicator(ctx.staging_dir, "adx")
    cand = fx.write_rsi_pullback(ctx.staging_dir, pins=None)
    import yaml
    cfg = yaml.safe_load((cand / "config.yaml").read_text())
    cfg["indicators"]["rsi"]["plugin"] = "adx"       # staging 内を指す
    (cand / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))

    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})

    assert out["started"] is False
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi" and out["reason"] == "not_found"
    assert out["available"] == ["rsi"]       # staging の adx は出ない
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_unresolved_run_backtest_leaves_the_backlog_last_result_untouched(
        tmp_path):
    """F4 (`last_result` 不変、**codex plan r1 I8**)。

    設計書 §6 F4 は counters と並べて **`last_result` 不変**を要求している
    (遮断 8: 未解決の RPC は改善 worker に渡る文字列を 1 文字も動かさない)。
    v1.2 はこれを 1 箇所も観測していなかった。**実 DB (tmp) の
    `improvement_backlog` 行に見張り値を入れ、handler を回した後に
    同じ値のままであること**を pin する (`last_result` を書くのは
    mission の終端 (`_finalize_*`) だけ、という規律の回帰にもなる)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": "a" * 64})  # pin 破れ
    # [indicator-consumption-wiring] T5b 逸脱是正: プラン本文は「backlog が
    # 空なら 1 行作ってから回すこと」と自ら注記していたが具体的な INSERT を
    # 書いていなかった (実測: `_improve_env`/`_prepare_ctx` だけでは
    # improvement_backlog は空)。`backlog_store.add` で見張り行を 1 本作る。
    from agentic_fx.store import backlog as backlog_store
    backlog_store.add(conn, "sentinel idea", "test", now=fx.NOW)
    conn.execute(
        "UPDATE improvement_backlog SET last_result='SENTINEL_UNCHANGED'")
    conn.commit()

    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})

    assert out["started"] is False
    rows = conn.execute("SELECT last_result FROM improvement_backlog").fetchall()
    assert rows, "見張り行が無い (backlog が空なら 1 行作ってから回すこと)"
    assert all(r["last_result"] == "SENTINEL_UNCHANGED" for r in rows)


def test_run_backtest_handler_accepts_unpinned_candidates(tmp_path):
    """探索中は `check` — pin 無しでも通る (提出時に `require` で落ちる)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins=None)
    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})
    assert out.get("started") is not False
    assert "metrics" in out


def test_run_backtest_handler_refuses_stale_pin_under_check(tmp_path):
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": "a" * 64})
    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})
    assert out["started"] is False and out["reason"] == "pin_mismatch"


def test_started_true_is_present_on_success(tmp_path):
    """F4 の裏: 成功応答にも `started` キーが載る (子が解放しない判定を
    キーの有無ではなく値で行えるように)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": hashes["rsi"]})
    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})
    assert out["started"] is True

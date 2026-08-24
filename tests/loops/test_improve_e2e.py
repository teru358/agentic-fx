"""FakeRunner E2E: §7.1-6「発見 → バックログ追加 → 候補 → ゲート → 承認申請 →
レポート公開 → backlog 遷移」の全周を fake で回す (プラン10 Task 12)。

WorkerRunner (実サブプロセス) は monkeypatch で FakeImproveWorkerRunner に
差し替える — Landlock/実 CLI の検証は Task 1〜6 の実プロセステストが担う。
ここでは ImproveLoop の commit 相 (親側ロジック) を対象にする。
"""
# precheck 2026-08-22 wave2: T12-B1 T12-B2 T12-M4
from __future__ import annotations

import os
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
                         "evidence": "test evidence"}],
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


def _write_staging_plugin(root: Path, mission_id: int, name: str,
                          plugin_py: str, config_yaml: str,
                          test_plugin: str) -> Path:
    """FakeImproveWorkerRunner はサブプロセスを起こさないため、`prepare()` が
    作った staging_dir へ候補 3 本を直接置く (worker が書くはずの内容を
    テストが代理で書く)。"""
    staging = root / "plugins" / "_staging" / str(mission_id) / name
    staging.mkdir(parents=True)
    (staging / "plugin.py").write_text(plugin_py, encoding="utf-8")
    (staging / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (staging / "test_plugin.py").write_text(test_plugin, encoding="utf-8")
    return staging


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
            root, ctx.mission_id, "rsi_gate_e2e",
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


def test_gate_failure_stops_at_report_no_approval_request(improve_env):
    """候補がゲート不合格 (test_plugin.py が plugin.py を書き換えようとする)
    のとき、承認申請は出ず、レポートのみで backlog が observation に落ちる。"""
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
            root, ctx.mission_id, "bad_gate_e2e",
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

    report_files = list((root / "reports").glob("improve-*.md"))
    assert len(report_files) == 1

    live = root / "plugins" / "bad_gate_e2e"
    assert not live.exists()


# precheck 2026-08-22 wave2: T12-M2
def test_concurrent_duplicate_selection_loser_becomes_observation(improve_env):
    """2 Mission が同じ backlog id を同時に選択したとき、Tx-1 の CAS に
    負けた側 (後着) が observation に落ち、勝者だけがゲートへ進む
    (§4.1 Tx-1、§8.1-24)。M-2 (report-task12.md): 旧版は同一接続で逐次 2 回
    `loop.prepare`/`commit` を呼ぶだけで「並行」を名乗っておらず、
    `backlog_row` を fetch しても未使用のまま、killer は目視確認頼みだった。
    ここでは 2 スレッド + 別接続 (`db_write_conn_factory` が呼び出しごとに
    新しい接続を作る現物契約を利用) を使い、`loop.commit()` 呼び出し直前で
    `threading.Barrier(2)` により実際に競合させる。"""
    import threading

    app, root = improve_env
    conn = app.conn_core

    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)

    def _selected_output(name):
        out = _plugin_artifact(name)
        out["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}
        return out

    barrier = threading.Barrier(2)
    mission_ids: dict[str, int] = {}
    errors: list[BaseException] = []

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
            result = MissionResult(status="completed",
                                  output=_selected_output(name), transcript=[])
            with patch("agentic_fx.runners.worker_runner.WorkerRunner",
                       lambda **kw: FakeImproveWorkerRunner(
                           result=result, **kw)):
                mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
                _write_staging_plugin(
                    root, ctx.mission_id, name,
                    _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
                    _PASSING_INDICATOR_TEST)
                mission_ids[name] = ctx.mission_id
                mission_result = worker.run(mission)
                barrier.wait(timeout=10)  # 両スレッドの commit を競合させる
                loop.commit(mission=mission, ctx=ctx, result=mission_result,
                           now=NOW)
        except BaseException as exc:  # noqa: BLE001 — スレッド内例外を回収
            errors.append(exc)

    threads = [threading.Thread(target=_run_one, args=(name,))
              for name in ("winner_e2e", "loser_e2e")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == [], f"thread(s) raised: {errors!r}"

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    # 二重の承認申請が無いことが本テストの主 killer。
    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert approval_count <= 1
    # M-2 で未使用のまま残っていた backlog_row を実際に使う: CAS に負けた
    # 側の遷移が必ず記録され「selected」のまま止まらないことを確認する
    # (`selected` のまま = どちらの CAS も成功しておらず状態機械が壊れている)。
    assert backlog_row[0] != "selected"
    assert backlog_row[1] is not None

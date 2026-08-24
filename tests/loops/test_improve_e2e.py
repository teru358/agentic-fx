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
    return dict(
        scope=overrides.get("scope", "in_sample"),
        plugin_ref=overrides.get("plugin_ref", "unknown:unknown"),
        content_hash=overrides.get("content_hash", "0" * 64),
        kind=overrides.get("kind", "strategy"),
        pair=overrides.get("pair", "USDJPY"),
        timeframe=overrides.get("eval_timeframe", overrides.get("timeframe", "1h")),
        source=overrides.get("source", "yfinance"),
        period=(overrides.get("period_start", NOW), overrides.get("period_end", NOW)),
        metrics={"trades": trades},
        settings_hash="fake-settings-hash",
        core_commit="fake-core-commit",
        initial_balance=settings.backtest.initial_balance,
        now=overrides.get("now", NOW),
    )


def _fake_in_sample_metrics(trades: int):
    """`run_in_sample(settings, *, history_conn, record_fn, ...)` の
    fake。§4.2-4 の evaluable 判定 (`trades >= EVALUABLE_MIN_TRADES`, 既存
    `backtest/metrics.py`) に合わせ、`record_fn` へ現物 `save_kwargs` 形の
    in_sample 行を 1 つ積んでから `{"trades": trades, "evaluable": trades >=
    30}` を返す。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        record_fn(_fake_record_kwargs(settings=settings, trades=trades,
                                      scope="in_sample", **kwargs))
        return {"trades": trades, "evaluable": trades >= 30}
    return _run


def _fake_holdout_metrics():
    """`run_holdout_gate` の fake。`record_fn` へ現物 `save_kwargs` 形の
    holdout 行を積む。①のシナリオ (trades<30) では呼ばれない想定 —
    呼ばれたら §4.2-4 の evaluable ゲートが壊れている。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        record_fn(_fake_record_kwargs(settings=settings, trades=40,
                                      scope="holdout_gate", **kwargs))
        return {"trades": 40, "evaluable": True}
    return _run


def test_approval_payload_analysis_ids_come_from_ledger_not_agent_claim(
        improve_env):
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
            root, ctx.mission_id, "ledger_pin_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
        # 台帳に RPC 呼出しを一切積まない (analyze_corr/run_backtest を
        # 呼ばない Mission) — 台帳は空のまま FROZEN になる
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)

    payload = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin'").fetchone()[0]
    assert "9999" not in payload
    assert "100000" not in payload
    assert '"trial_count": 0' in payload or "'trial_count': 0" in payload


def test_strategy_below_evaluable_min_trades_becomes_observation(improve_env):
    """§4.2-4: strategy artifact の合計取引数が `EVALUABLE_MIN_TRADES` (=30)
    未満なら承認申請を出さず observation に落ちる (`insufficient_trades:<n>`)。
    `run_holdout_gate` は呼ばれない (評価不能で holdout に進まないこと自体が
    このテストの killer)。"""
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
            root, ctx.mission_id, "low_trades_strategy_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
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


def test_artifact_name_traversal_fails_mission(improve_env):
    """§4.2-1: `artifact.name` が正規形 (`^[a-z][a-z0-9_]{0,63}$`) でない
    (`../` を含む) 候補は出力検査で不合格になり、Mission は `failed`。
    Tx-1 (backlog 追記・選択) には一切進まない — backlog 行は 0 件のまま
    (敗者経路とも異なり、遷移そのものが起きない)。"""
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
        "SELECT COUNT(*) FROM improvement_backlog").fetchone()[0]
    assert backlog_count == 0

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
            root, ctx.mission_id, "bad_gate_symlink_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)  # ゲート不合格 → レポート経路へ入る
        tmp_dir = root / "reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        evil_target = root / "reports" / ".tmp" / "evil-target.md"
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
        # ctx.rpc_handlers を実際に叩いてから timeout を確定させる —
        # 台帳に entries が積まれた状態でも DISCARD されることを確認する。
        if "run_backtest" in ctx.rpc_handlers:
            try:
                ctx.rpc_handlers["run_backtest"]({"pair": "USDJPY"})
            except Exception:
                pass  # fake 引数が不正でも呼出し自体が台帳へ積まれれば良い
        if "analyze_corr" in ctx.rpc_handlers:
            try:
                ctx.rpc_handlers["analyze_corr"]({"pairs": ["USDJPY"]})
            except Exception:
                pass
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
    tmp_dir = root / "reports" / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.chmod(0o500)  # 書込不可 (自分の書込みビットだけ落とす)
    try:
        with patch("agentic_fx.runners.worker_runner.WorkerRunner",
                   lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
            mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
            _write_staging_plugin(
                root, ctx.mission_id, "report_write_fails_e2e",
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
    docstring 参照)。"""
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
            root, ctx.mission_id, "no_baseline_strategy_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _PASSING_INDICATOR_TEST)
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
            root, ctx1.mission_id, "outbox_ok_e2e",
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
    assert not (root / "reports" / ".tmp" /
               f"improve-{ctx1.mission_id}.md.part").exists()

    # --- 2 本目: 最終名を先取りして rename 公開を失敗させる ---
    output2 = _plugin_artifact("outbox_fail_e2e")
    result2 = MissionResult(status="completed", output=output2, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result2, **kw)):
        mission2, ctx2, worker2 = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            root, ctx2.mission_id, "outbox_fail_e2e",
            _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
            _FAILING_INDICATOR_TEST)
        expected_final = root / "reports" / f"improve-{NOW:%Y-%m-%d}-{ctx2.mission_id}.md"
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

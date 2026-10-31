"""同名更新の比較 gate を、実 worker・実 DB で通すための環境。

`build_app` で組んだ本番同型の app に、決定論の合成履歴と配備済みの
strategy を置き、改善 mission を 3 入口 (週次 / 手動単発 / 手動 repeat) から
実 `ImproveLoop`・実 `ImproveSupervisor` で走らせる。差し替えるのは、LLM を
呼ぶ `WorkerRunner` (候補の置き込みと固定の出力を返す) だけで、gate の
backtest は plugin sandbox の本物の worker プロセスで動く。

全て `tmp_path` 配下に作る。実 DB・実 `plugins/`・`data/` には触れない。
"""
from __future__ import annotations

import itertools
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_waves
from agentic_fx.store.db import connect, connect_readonly
from tests.fixtures import indicator_wiring as fx
from tests.loops.test_improve_e2e import (
    FakeImproveWorkerRunner, _MIN3_TEST_PY, _install, _plugin_artifact,
    _write_staging_plugin)
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _no_real_network

NOW = fx.NOW
NAME = "momo"
PAIR = fx.PAIR

CONFIG = (
    "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
    "max_bars: 50\nparams: {}\n")


def strategy_py(take_profit: float = 0.6, stop_loss: float = 0.3, *,
                note: str = "", min_close: float | None = None,
                max_close: float | None = None) -> str:
    """3 本連続で上昇したら long を出す、依存なしの strategy。

    合成履歴 (周期 24 時間の正弦波) の上で、`take_profit` / `stop_loss` の組ごとに
    決定論の成績になる。`note` は未使用の定数 1 行 (AST と hash は変わるが挙動は変わらない)。
    `min_close` / `max_close` は取引を減らすための価格帯の絞り込み。"""
    band = ""
    if min_close is not None:
        band += f"        if close < {min_close!r}:\n            return hold\n"
    if max_close is not None:
        band += f"        if close > {max_close!r}:\n            return hold\n"
    marker = f"_NOTE = {note!r}\n\n" if note else ""
    return (
        "import pandas as pd\n\n"
        f"{marker}\n"
        "def evaluate(df, indicators, signals, params):\n"
        "    hold = {'action': 'hold', 'rationale': 'flat'}\n"
        "    c = df['close'].astype(float)\n"
        "    if len(c) < 3:\n"
        "        return hold\n"
        "    if c.iloc[-1] > c.iloc[-2] > c.iloc[-3]:\n"
        "        close = float(c.iloc[-1])\n"
        f"{band}"
        "        return {'action': 'open', 'direction': 'long',\n"
        "                'entry_type': 'market',\n"
        f"                'stop_loss': close - {stop_loss!r},\n"
        f"                'take_profit': close + {take_profit!r},\n"
        "                'rationale': 'up'}\n"
        "    return hold\n")


@dataclass
class World:
    app: object
    root: Path
    loop: ImproveLoop
    supervisor: ImproveSupervisor
    runs: list = field(default_factory=list)

    @property
    def conn(self):
        return self.app.conn_core

    @property
    def settings(self):
        return self.app.settings


@contextmanager
def open_world(root: Path, *, mode: str = "enforce",
               wall_clock_max_sec: float | None = None,
               plugin_updates: dict | None = None,
               gate_updates: dict | None = None):
    """合成履歴を入れた app と、実 loop / 実 supervisor を返す。"""
    root.mkdir(parents=True, exist_ok=True)
    _install(root)
    with _no_real_network():
        app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
        try:
            s = app.settings
            s.backtest.holdout_months = fx.HOLDOUT_MONTHS
            s.backtest.eval_source = fx.SOURCE
            s.backtest.base_interval = fx.BASE_INTERVAL
            s.improve.baseline_gate = mode
            if wall_clock_max_sec is not None:
                s.improve.baseline_replay_wall_clock_max_sec = wall_clock_max_sec
            for key, value in (plugin_updates or {}).items():
                setattr(s.plugin, key, value)
            for key, value in (gate_updates or {}).items():
                setattr(s.improve.gate, key, value)
            fx.seed_history(app.conn_core)
            db_path = root / "data" / "agentic.db"
            loop = ImproveLoop(
                root=root, settings=s, clock=FixedClock(NOW),
                db_write_conn_factory=lambda: connect(db_path),
                db_readonly_conn_factory=lambda: connect_readonly(db_path),
                activity=app.activity, rag=app.rag)
            sup = ImproveSupervisor(
                capacity=1, root=root, settings=s, clock=FixedClock(NOW),
                db_path=db_path, stop_event=threading.Event(),
                activity=app.activity, emit_events=False)
            sup._improve_loop = loop
            yield World(app=app, root=root, loop=loop, supervisor=sup)
        finally:
            app.close()


def deploy(world: World, name: str = NAME, py: str | None = None,
           config: str = CONFIG) -> str:
    """`name` を承認済みの配備として置く。戻り値は content_hash。"""
    d = world.root / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(py or strategy_py(), encoding="utf-8")
    (d / "config.yaml").write_text(config, encoding="utf-8")
    (d / "test_plugin.py").write_text(_MIN3_TEST_PY, encoding="utf-8")
    return fx.deploy_approved(world.conn, world.root / "plugins", [name],
                              now=NOW)[name]


class _StagingRunner(FakeImproveWorkerRunner):
    """worker が staging に書くはずの候補を、run の中で代わりに置く。"""

    def __init__(self, *, files, before_run=None, after_stage=None, **kwargs):
        super().__init__(**kwargs)
        self._files = files
        self._before_run = before_run
        self._after_stage = after_stage

    def run(self, mission):
        ctx = self.kwargs["run_context"]
        if self._before_run is not None:
            self._before_run(ctx)
        if self._files is not None:
            _write_staging_plugin(ctx.staging_dir, *self._files)
        if self._after_stage is not None:
            self._after_stage(ctx)
        return super().run(mission)


def candidate_files(name: str = NAME, py: str | None = None,
                    config: str = CONFIG):
    return (name, py or strategy_py(), config, _MIN3_TEST_PY)


_IDEA_SEQ = itertools.count(1)


def _output_for(name: str) -> dict:
    """mission ごとに違う idea を選んだ出力 (同じ idea は重複として見送られる)。"""
    output = _plugin_artifact(name, kind="strategy")
    idea = f"idea {next(_IDEA_SEQ)} for {name}"
    output["discoveries"][0]["idea"] = idea
    output["selected"]["idea"] = idea
    return output


@contextmanager
def staged(files_list, *, name: str = NAME, before_run=None,
           after_stage=None):
    """`WorkerRunner` を、`files_list` の先頭から順に候補を置くものに差し替える。"""
    queue = list(files_list)

    def factory(**kw):
        files = queue.pop(0) if queue else None
        result = MissionResult(status="completed", output=_output_for(name),
                               transcript=[])
        return _StagingRunner(files=files, result=result,
                              before_run=before_run,
                              after_stage=after_stage, **kw)

    with patch("agentic_fx.runners.worker_runner.WorkerRunner", factory):
        yield


def run_manual(world: World, files, *, before_run=None,
               after_stage=None) -> int:
    with staged([files], name=files[0], before_run=before_run,
                after_stage=after_stage):
        return world.supervisor.submit_manual()


def run_weekly(world: World, files, *, period: str = "2026-W09",
               before_run=None) -> int:
    """週次 slot を 1 本作って起動する。戻り値は mission id。"""
    backlog_store.add(world.conn, f"idea-{period}", "user", NOW)
    improve_waves.create_wave_and_slots(
        world.conn, period_key=period, now=NOW, expected=1, commit=True)
    before = {r[0] for r in world.conn.execute("SELECT id FROM missions")}
    with staged([files], name=files[0], before_run=before_run):
        world.supervisor._launch_slot(period, 0)
    (mid,) = {r[0] for r in world.conn.execute("SELECT id FROM missions")} - before
    return mid


def run_repeat(world: World, files_list, *, max_runs: int | None = None):
    """手動 repeat。戻り値は (RepeatResult, progress の記録)。"""
    progress: list[dict] = []
    cancel = threading.Event()
    # 失敗の後の待ち (既定 300 秒) を短くしておく。正しい実装は待たない。
    with staged(files_list, name=files_list[0][0]), \
            patch("agentic_fx.core.improve_repeat.BACKOFF_SEC", 1.0):
        result = world.supervisor.submit_repeat(
            duration_sec=3600, max_runs=max_runs or len(files_list),
            comment_holder=None, on_progress=progress.append,
            cancel_event=cancel, autopilot_check=lambda: False)
    return result, progress


# ---- afx-ops (別 venv) の描画 -------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
AFX_OPS_PYTHON = _REPO / "afx-ops" / ".venv" / "bin" / "python"
_PROBE = Path(__file__).resolve().parent / "afx_ops_probe.py"


def afx_ops_render(request: dict) -> dict:
    """afx-ops の venv の python で描画し、`{"text": ...}` を返す。"""
    import json
    import subprocess
    done = subprocess.run(
        [str(AFX_OPS_PYTHON), str(_PROBE)], input=json.dumps(request),
        capture_output=True, text=True, timeout=60, cwd=str(_REPO / "afx-ops"),
        check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


# ---- 1 mission 後の観測 ---------------------------------------------------------

def _all(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


def snapshot(world: World, mission_id: int) -> SimpleNamespace:
    """mission 終端後に、DB・activity・レポートから観測点をまとめて読む。"""
    conn = world.conn
    notice = conn.execute(
        "SELECT result_code, reason_code FROM improve_run_notices "
        "WHERE mission_id=?", (mission_id,)).fetchone()
    status = conn.execute("SELECT status FROM missions WHERE id=?",
                          (mission_id,)).fetchone()
    approvals = [p for p in (json.loads(r["payload_json"])
                             for r in conn.execute(
        "SELECT payload_json FROM approval_requests WHERE status='pending' "
        "ORDER BY id")) if p.get("mission_id") == mission_id]
    activity = world.app.activity.tail(10_000)
    reports = list((world.root / "data" / "improve_reports").glob(
        f"improve-*-{mission_id}.md"))
    return SimpleNamespace(
        mission_id=mission_id,
        status=status[0] if status else None,
        notice=tuple(notice) if notice else None,
        rows=_all(conn, "SELECT variant, scope, pair, mission_outcome, "
                        "plugin_ref, content_hash, ref_content_hash, "
                        "metrics_json FROM backtest_runs WHERE mission_id=? "
                        "ORDER BY id", mission_id),
        approvals=approvals,
        activity=activity,
        archives=[r["outcome_class"] for r in _all(
            conn, "SELECT outcome_class FROM candidate_archives "
                  "WHERE mission_id=?", mission_id)],
        backlog=_all(conn, "SELECT last_result, origin_outcome FROM "
                           "improvement_backlog WHERE origin_mission_id=?",
                     mission_id),
        report=(reports[0].read_text(encoding="utf-8") if reports else None))


def variants(snap) -> list[str]:
    return [r["variant"] for r in snap.rows]


def run_scenario(root: Path, files, *, deployed_py: str | None = None,
                 deployed_config: str = CONFIG, mode: str = "enforce",
                 wall_clock_max_sec: float | None = None,
                 plugin_updates: dict | None = None,
                 gate_updates: dict | None = None, wrap=None,
                 after_stage=None, expect_error=None):
    """新しい world に配備済みの版を置き、手動 mission を 1 本通して観測を返す。

    `wrap(deployed_hash)` は mission の間だけ有効な context manager を返す
    (環境の差し替え用)。`expect_error` に例外型を渡すと、その例外が
    mission から伝播することも確かめる。戻り値は `(snapshot, deployed_hash,
    error)`。"""
    import contextlib
    with open_world(root, mode=mode, wall_clock_max_sec=wall_clock_max_sec,
                    plugin_updates=plugin_updates,
                    gate_updates=gate_updates) as world:
        deployed = deploy(world, py=deployed_py, config=deployed_config)
        error = None
        before = {r[0] for r in world.conn.execute("SELECT id FROM missions")}
        ctx = wrap(deployed) if wrap else contextlib.nullcontext()
        with ctx:
            try:
                run_manual(world, files, after_stage=after_stage)
            except Exception as exc:  # noqa: BLE001
                if expect_error is None or not isinstance(exc, expect_error):
                    raise
                error = exc
        (mission,) = {r[0] for r in world.conn.execute(
            "SELECT id FROM missions")} - before
        if expect_error is not None:
            assert error is not None, "the mission was expected to raise"
        return snapshot(world, mission), deployed, error


@contextmanager
def timed_backtests(log: list):
    """gate の in_sample / holdout の各呼び出しを、通したまま実測する。
    `log` に `{"scope", "content_hash", "seconds", "raised"}` を積む。"""
    import time

    from agentic_fx.backtest import holdout
    real_in, real_ho = holdout.run_in_sample, holdout.run_holdout_gate

    def wrap(scope, real):
        def run(*args, **kwargs):
            start = time.monotonic()
            raised = None
            try:
                return real(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                raised = type(exc).__name__
                raise
            finally:
                log.append({"scope": scope,
                            "content_hash": kwargs.get("content_hash"),
                            "seconds": time.monotonic() - start,
                            "raised": raised})
        return run

    with patch("agentic_fx.backtest.holdout.run_in_sample",
               wrap("in_sample", real_in)), \
            patch("agentic_fx.backtest.holdout.run_holdout_gate",
                  wrap("holdout", real_ho)):
        yield


def agent_run_backtest(ctx, name: str, pair: str = PAIR) -> str:
    """agent が tool として `run_backtest` を 1 回呼んだ経路 (台帳・予算・registry を
    通る)。戻り値は agent が受け取る返却の文字列。"""
    from agentic_fx.config import ImproveToolBudgetSettings
    from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs
    from agentic_fx.tools.mission_counters import MissionToolCounters
    from agentic_fx.tools.registry import ToolRegistry
    budget = ImproveToolBudgetSettings()
    counters = MissionToolCounters(budget=budget)
    defs = build_improve_rpc_tooldefs(
        ledger=ctx.ledger, run_backtest_handler=ctx.rpc_handlers["run_backtest"],
        analyze_corr_handler=ctx.rpc_handlers["analyze_corr"],
        staging_dir=ctx.staging_dir, counters=counters, budget=budget)
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)
    return registry.execute("run_backtest", {"name": name, "pair": pair},
                            allowed=registry.names())

"""[indicator-consumption-wiring] テスト環境ビルダ (T3〜T5 共有)。

**実 DB・実 `plugins/`・`docs/examples/` を書き換えない** — 全て `tmp_path`
配下に作る。各テストモジュールは名前を `_` 付きで別名 import してよい
(例: `from tests.fixtures.wiring_envs import switch_env as _switch_env`)。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from agentic_fx.config import load_settings
from agentic_fx.store.db import connect, connect_readonly, init_db
from tests.fixtures import indicator_wiring as fx

_REPO = Path(__file__).resolve().parents[2]

# opus r1 (未確認 #9 への予防): 元案は `load_settings(...)` の戻りを**その場で
# mutate** していた。`Settings` は pydantic モデルで既定 mutable なので動くが、
# module-level の共有オブジェクトを書き換える形はテスト間汚染の温床。
# `model_copy` で新しい object を作る (着手時に `rg -n 'class Settings' src/agentic_fx/config.py`
# で pydantic v2 であることと `backtest` のフィールド名を再確認すること)。
SETTINGS_FIXTURE = load_settings(_REPO / "config" / "settings.yaml.example").model_copy(
    deep=True)
SETTINGS_FIXTURE.backtest = SETTINGS_FIXTURE.backtest.model_copy(
    update={"holdout_months": fx.HOLDOUT_MONTHS, "eval_source": fx.SOURCE,
            "base_interval": fx.BASE_INTERVAL})


class _FakeRag:
    """`ImproveLoop.__init__(rag=...)` を満たすだけの no-op。
    `tests/loops/conftest.py` の `_FakeRag` からの逐語転写
    (手書きの偽形状は禁止 — [[test-fixtures-from-real-transcripts]])。"""


def _db(root: Path):
    (root / "data").mkdir(parents=True, exist_ok=True)
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    return conn


def switch_env(root: Path):
    """`(conn, plugins_root)`。`plugins/` と `plugins/_human` を作る。"""
    root.mkdir(parents=True, exist_ok=True)
    conn = _db(root)
    plugins_root = root / "plugins"
    (plugins_root / "_human").mkdir(parents=True, exist_ok=True)
    return conn, plugins_root


def reconcile_env(root: Path):
    """`(conn, plugins_root, activity)`。activity は `ActivityLog`。"""
    from agentic_fx.activity import ActivityLog
    conn, plugins_root = switch_env(root)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return conn, plugins_root, ActivityLog(root / "logs" / "activity.log")


def improve_env(root: Path):
    """`(loop, conn, root)`。`ImproveLoop` を本番同型で組む。

    **opus r1 C2 / I6 是正**。元案には 2 つの欠陥があった:

    1. `ImproveLoop.__init__` は
       `(*, root, settings, clock, db_write_conn_factory, db_readonly_conn_factory,
       activity: ActivityLog, rag: Rag)` で **`activity` / `rag` も必須**
       (着手時に `rg -n 'def __init__' -A 12 src/agentic_fx/loops/improve_loop.py`
       で再取得)。5 引数だけでは `TypeError`。
    2. write/readonly の両 factory に**同一の 1 本の conn** を返すと、
       親の `run_backtest_handler` が `finally: conn.close()` する既存構造
       (Step 5-4c 参照) により 1 回 backtest を回した時点でテスト側の
       `conn` まで閉じ、以降の `conn.execute(...)` が
       `ProgrammingError: Cannot operate on a closed database` になる。

    本番 (`service.py` の配線) と同型の「毎回 db_path へ新規接続」にし、
    テストが握る `conn` は**別に 1 本**開く。`tests/loops/conftest.py` の
    `loop_no_seam` fixture がこの形の現物なので逐語転写する
    (`_conn_for_test` seam は**立てない** — seam を立てると prepare が
    conn を閉じない代わりに上記 1 本共有に戻ってしまう)。
    """
    from agentic_fx.activity import ActivityLog
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.loops.improve_loop import ImproveLoop
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "plugins").mkdir(exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    db_path = root / "data" / "agentic.db"
    bootstrap = connect(db_path)
    init_db(bootstrap)
    bootstrap.close()
    conn = connect(db_path)          # テストが握る接続 (loop のものとは別)
    loop = ImproveLoop(root=root, settings=SETTINGS_FIXTURE,
                       clock=FixedClock(fx.NOW),
                       db_write_conn_factory=lambda: connect(db_path),
                       db_readonly_conn_factory=lambda: connect_readonly(db_path),
                       activity=ActivityLog(root / "logs" / "activity.log"),
                       rag=_FakeRag())
    return loop, conn, root


def improve_env_with_activity(root: Path):
    """`(loop, conn, root, activity)`。`activity` は `improve_env` が
    `ImproveLoop(activity=...)` へ渡した `ActivityLog` そのもの
    (`ImproveLoop.__init__` が `self._activity = activity` を持つのは現物で
    確認済み — 着手時に `rg -n '_activity' src/agentic_fx/loops/improve_loop.py`
    で再確認する)。"""
    loop, conn, root = improve_env(root)
    return loop, conn, root, loop._activity


def prepare_ctx(loop, *, now):
    """`ImproveRunContext` を 1 本で取る (**opus r1 C1 是正**)。

    現物のシグネチャは
    `prepare(self, *, slot_key: tuple[str, int] | None, now: datetime,
    on_ready: Callable[[dict], None] | None = None) -> tuple[Mission, ImproveRunContext, WorkerRunner]`
    (着手時に `rg -n 'def prepare' -A 4 src/agentic_fx/loops/improve_loop.py`
    で再取得)。すなわち **`conn` 引数は無く** (conn は
    `db_write_conn_factory` から自前で取る)、**`slot_key` はキーワード必須**、
    **戻り値は 3-tuple で ctx は 2 番目**。プラン v1 が全テストで書いていた
    `ctx = _prepare_ctx(loop, now=...)` は
    `TypeError: prepare() got an unexpected keyword argument 'conn'` で即死する。
    T5 の全テストは**このヘルパ経由**にすること。

    `prepare` は副作用として `WorkerRunner` を構築する。tmp 環境で成立する
    ことは T6b の smoke test (`test_prepare_ctx_builds_a_run_context`) で
    確かめる — 成立しない場合のみ `ImproveRunContext` 直組みの
    `synthetic_ctx` (下記) に切り替える。
    """
    _mission, ctx, _runner = loop.prepare(slot_key=None, now=now)
    return ctx


def synthetic_ctx(loop, conn, root: Path):
    """`prepare` を経由せず `ImproveRunContext` を直組みする fallback。
    `tests/loops/conftest.py` の `loop_and_ctx` fixture からの逐語転写。
    **`prepare_ctx` が tmp 環境で成立する限り使わない** (T6b の smoke test が
    判定する)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.loops.improve_run_context import ImproveRunContext
    staging_dir = root / "staging"
    source_snapshot_dir = root / "source"
    staging_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot_dir.mkdir(parents=True, exist_ok=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": 600.0, "analyze_corr": 600.0})
    return ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        allowed_backlog_ids=frozenset(), slot_key=None, ledger=ledger,
        rpc_handlers={})


def activity_text(activity) -> str:
    """activity ログの全文 (**opus r1 C3 是正**)。

    `ActivityLog` の公開メソッドは `write(category, event, summary, ref_id=None)`
    と `tail(n=20, category=None)` **だけ** — `read_text()` は存在しない
    (プラン v1 は 4 箇所で `activity.read_text()` を呼んでおり全部
    `AttributeError`)。逐語 pin はこのヘルパ経由で行う。

    **行の形 (逐語)**: `ActivityLog.write` は
    `"\t".join([ts_iso_seconds, category.value, event, " ".join(summary.split()), ref_id or "-"])`
    を 1 行として書く。つまり受入の「逐語」は**行全体の完全一致ではなく
    `event` と `summary` 部分の一致**で見る (先頭 2 列は時刻とカテゴリ)。
    T6b の smoke test (`test_activity_text_returns_written_lines`) で
    この形を 1 度だけ確かめてから 4 箇所へ展開すること。
    """
    return "\n".join(activity.tail(10_000))


def loop_env(root: Path):
    """`(loop, conn, plugins_root)` — 質検査 (P5) 用の薄い別名。"""
    loop, conn, root = improve_env(root)
    return loop, conn, root / "plugins"


def shell_env(root: Path):
    """`(cmds, conn, plugins_root)` — `commands.Commands` (**opus r1 C4 是正**)。

    `agentic_fx.commands` にあるクラスは `Shell` ではなく **`Commands`** で、
    `__init__` は `conn / state_store / broker / trade_loop / activity /
    log_dir / clock` が**すべて必須**、`root` 引数は存在しない
    (`plugins_root` / `settings` / `health_latch` などが任意)。
    `tests/test_commands.py::_commands` の組み方を逐語転写する。
    `_approval_detail` (Step 4-8c) は `self.plugins_root` と `self.settings` を
    読むので両方渡す。
    """
    from unittest.mock import MagicMock

    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.core.health_latch import HealthLatch
    from agentic_fx.core.paper_broker import PaperBroker
    from agentic_fx.store.state import StateStore
    conn, plugins_root = switch_env(root)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    clock = FixedClock(fx.NOW)
    cmds = Commands(
        conn=conn, state_store=StateStore(root / "s.json"),
        broker=PaperBroker(conn, SETTINGS_FIXTURE, clock),
        trade_loop=MagicMock(),
        activity=ActivityLog(root / "logs" / "activity.log"),
        log_dir=root / "logs", clock=clock, health_latch=HealthLatch(),
        plugins_root=plugins_root, settings=SETTINGS_FIXTURE)
    return cmds, conn, plugins_root


def rpc_tooldefs(root: Path, *, counters, run_backtest_handler) -> list:
    """`improve_rpc_tools.build_improve_rpc_tooldefs` の薄いラッパ
    (staging_dir 検証を通すため候補を 1 本置く)。**`ToolDef` のリスト**を
    返すので、`ToolRegistry(on_execute=counters.record_call,
    on_result=counters.record_tool_result)` を作って `register_all(defs)` で
    登録し `execute(name, args, allowed=registry.names())` で呼べる
    (`ToolRegistry.__init__` はキーワード専用で tooldef を取らない —
    `mission_registry.py` の構築行の逐語)。F4 の `errors` / refusal streak は
    registry の `on_result` 経由でしか増えないため必要 (opus r1 I5)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.tools import improve_rpc_tools
    staging = root / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    (staging / "cand").mkdir(parents=True, exist_ok=True)
    shutil.copytree(staging / "rsi_pullback", staging / "cand",
                    dirs_exist_ok=True)
    return improve_rpc_tools.build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 60,
                                                         "analyze_corr": 60}),
        run_backtest_handler=run_backtest_handler,
        analyze_corr_handler=lambda a: {},
        staging_dir=staging, counters=counters,
        budget=SETTINGS_FIXTURE.improve.tool_budget)


def rpc_tools(root: Path, *, counters, run_backtest_handler):
    """`{tool 名: func}` — tooldef を**直接呼ぶ**経路 (registry を通らない)。"""
    return {d.name: d.func for d in
            rpc_tooldefs(root, counters=counters,
                         run_backtest_handler=run_backtest_handler)}


def deploy_strategy(conn, plugins_root: Path, name: str, *, pins: dict) -> str:
    """`rsi_pullback` 相当の strategy を `name` で配備 (承認 + 正規形 symlink)。
    戻り値 = `content_hash`。"""
    from agentic_fx.plugin.loader import content_hash
    src = fx.write_rsi_pullback(plugins_root, pins=pins)
    if name != "rsi_pullback":
        dest = plugins_root / name
        shutil.copytree(src, dest)
        shutil.rmtree(src)
        src = dest
    chash = content_hash(src)
    fx.deploy_approved(conn, plugins_root, [name], now=fx.NOW)
    return chash


def bump_indicator_version(conn, plugins_root: Path, name: str, *,
                           now: datetime) -> str:
    """配備済 indicator を「出力不変の変更」で I2 に更新し、承認して
    live symlink を差し替える。戻り値 = 新 `content_hash`。"""
    from agentic_fx.plugin.loader import artifact_hash_bytes, content_hash
    from agentic_fx.store import approvals
    current = (plugins_root / name).resolve()
    plugin_py = current.read_bytes() if current.is_file() else \
        (current / "plugin.py").read_bytes()
    new_py = plugin_py + b"\n# v2 (output-preserving change)\n"
    config = (current / "config.yaml").read_bytes()
    test_py = (current / "test_plugin.py").read_bytes()
    ahash = artifact_hash_bytes(new_py, config, test_py)
    version_dir = plugins_root / ".versions" / name / ahash
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "plugin.py").write_bytes(new_py)
    (version_dir / "config.yaml").write_bytes(config)
    (version_dir / "test_plugin.py").write_bytes(test_py)
    link = plugins_root / name
    if link.is_symlink():
        link.unlink()
    link.symlink_to(Path(".versions") / name / ahash)
    chash = content_hash(version_dir)
    aid = approvals.create(conn, "plugin",
                           {"name": name, "kind": "indicator",
                            "content_hash": chash}, now)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t",
                             now=now)
    return chash


def submit_indicator_v2(conn, plugins_root: Path, name: str):
    """I2 を `plugins/_human/<name>` から submit する (pending のまま)。
    戻り値 = `(approval_id, content_hash)`。"""
    from agentic_fx.plugin import switch as plugin_switch
    from agentic_fx.plugin.loader import content_hash
    human = plugins_root / "_human" / name
    shutil.copytree((plugins_root / name).resolve(), human, dirs_exist_ok=True)
    (human / "plugin.py").write_text(
        (human / "plugin.py").read_text() + "\n# v2\n")
    approval_id = plugin_switch.submit_candidate(
        conn, name=name, staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    return approval_id, content_hash(human)


def approve_indicator_v2(conn, plugins_root: Path, name: str, *,
                         approval_id: int) -> None:
    from agentic_fx.plugin import switch as plugin_switch
    plugin_switch.approve_candidate(
        conn, approval_id, decided_by="human_cli", now=fx.NOW,
        plugins_root=plugins_root, settings=SETTINGS_FIXTURE)


def stage_switched_journal(conn, plugins_root: Path, *, name: str, pins: dict,
                           now: datetime):
    """`switched` 段で止まった journal を再現する
    (版 dir 作成 + live symlink 差し替え + journal phase=switched、DB は
    まだ `decided` にしない)。戻り値 =
    `(old_target, new_target, approval_id, op_id)`。

    **opus r1 I8 是正 (2 点)**:

    1. 元案は新 version dir に `plugin.py + "\n# next\n"` を書きながら
       approval payload には**旧** `content_hash` を入れていた。
       reconcile → `retry_approval` → `approve_candidate` →
       `_version_dir_hashes_ok(version_dir, content_hash=payload["content_hash"], ...)`
       が不一致で落ちるため、R2 の裏テスト
       (`test_switched_journal_with_intact_pin_proceeds_to_decided`) が
       そもそも `decided` に到達しない。**payload の `content_hash` は
       新 version dir の実体から `loader.content_hash(version_dir)` で
       算出する** (`artifact_hash` は既に `artifact_hash_bytes` で
       新実体から算出しているので整合する)。
    2. `advance_switch_journal(...)` は既定 `commit=False` なので、
       phase を書いても**同一 conn の外からは見えない**。
       `commit=True` を明示する (`begin_switch_journal` 側は既に
       `commit=True`)。

    このヘルパは R2 の成否を丸ごと決めるので、**T6b で「実際に reconcile が
    `decided` まで進む」smoke test を 1 本据えてから** T4b Step 4-7 へ渡すこと。
    """
    from agentic_fx.plugin import switch as plugin_switch
    from agentic_fx.plugin.loader import artifact_hash_bytes
    from agentic_fx.store import approvals
    old_hash = deploy_strategy(conn, plugins_root, name, pins=pins)
    old_target = f".versions/{name}/{(plugins_root / name).readlink().name}"
    human = plugins_root / "_human" / name
    shutil.copytree((plugins_root / name).resolve(), human, dirs_exist_ok=True)
    (human / "plugin.py").write_text(
        (human / "plugin.py").read_text() + "\n# next\n")
    ahash = artifact_hash_bytes((human / "plugin.py").read_bytes(),
                                (human / "config.yaml").read_bytes(),
                                (human / "test_plugin.py").read_bytes())
    new_target = f".versions/{name}/{ahash}"
    version_dir = plugins_root / new_target
    version_dir.mkdir(parents=True, exist_ok=True)
    for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
        (version_dir / rel).write_bytes((human / rel).read_bytes())
    from agentic_fx.plugin.loader import content_hash as _content_hash
    new_hash = _content_hash(version_dir)   # opus r1 I8: 新実体から算出
    assert new_hash != old_hash, (
        "stage_switched_journal: 新 version dir の content_hash が旧と同じ "
        "— plugin.py への追記が効いていない")
    approval_id = approvals.create(
        conn, "plugin",
        {"name": name, "kind": "strategy", "candidate_origin": "human",
         "candidate_path": f"plugins/_human/{name}",
         "content_hash": new_hash, "artifact_hash": ahash}, now)
    op_id = plugin_switch.begin_switch_journal(
        conn, kind="approve", approval_id=approval_id, name=name,
        old_kind="symlink", old_target=old_target, new_target=new_target,
        switch_required=True, actor="human_cli", now=now, commit=True)
    plugin_switch.switch_live(plugins_root, name, new_target=new_target,
                              op_id=op_id)
    plugin_switch.advance_switch_journal(conn, op_id, phase="switched", now=now,
                                        commit=True)   # opus r1 I8
    return old_target, new_target, approval_id, op_id


def copy_example(dest_root: Path, name: str) -> Path:
    """`docs/examples/plugins/<name>` を `dest_root/<name>` へ逐語コピー
    (`__pycache__` は除く)。"""
    dest = dest_root / name
    dest.mkdir(parents=True, exist_ok=True)
    src = _REPO / "docs" / "examples" / "plugins" / name
    for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
        (dest / rel).write_bytes((src / rel).read_bytes())
    return dest


def rename_dependency(plugin_dir: Path, old: str, new: str) -> None:
    """候補 `config.yaml` の `indicators.*.plugin` を置換する。"""
    path = plugin_dir / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    for ref in (config.get("indicators") or {}).values():
        if isinstance(ref, dict) and ref.get("plugin") == old:
            ref["plugin"] = new
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def write_dependency_free_strategy(base: Path, name: str) -> Path:
    """`indicators:` を持たない strategy 候補 (`indicator_deps == {}` の pin 用)。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(
        "def evaluate(df, indicators, signals, params):\n"
        "    return {'action': 'hold', 'rationale': 'no dependency'}\n")
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "strategy", "timeframe": "1h", "pairs": [fx.PAIR],
         "exit_mode": "levels", "max_bars": 200, "params": {}},
        sort_keys=False))
    (d / "test_plugin.py").write_text(
        "from plugin import evaluate\n\n\n"
        "def test_holds():\n"
        "    assert evaluate(None, {}, None, {})['action'] == 'hold'\n")
    return d


def completed_result(output: dict):
    """`MissionResult(status='completed', output=output)`。"""
    from agentic_fx.runners.base import MissionResult
    return MissionResult("completed", output, [], reason=None)


def mission_for(ctx):
    """`ImproveLoop.commit(mission=...)` に渡す最小の Mission。"""
    from agentic_fx.runners.base import Mission
    return Mission(prompt="", tools=[], output_schema=None, max_turns=1,
                   timeout_sec=60)


def install_gate_double(monkeypatch, *, pytest_ok: bool = True):
    """[codex plan r1 C1] strategy gate (= 実 backtest) を差し替える。

    **なぜ必要か**: `switch_env` は空 DB しか作らないのに、`submit_candidate`
    は実 `_run_full_gate` → `run_kind_gate` → `evaluate_strategy_adoption_gate`
    から **実 backtest** を回す。履歴が無ければ `NoHistoryError` で、履歴が
    あれば 3 ヶ月 × 1h の実 worker 実行で数分かかる。**承認回廊の lock 順序 /
    payload 形状 / 再解決タイミングを見る受入 (A2 / P2' / P2'' / P3 / P3') は
    gate の中身を見ていない**ので、gate を double にして高速・決定論にする。

    **保たれるもの** (double にしても観測点が死なない理由): `run_kind_gate` の
    `resolve_indicator_deps(...)` 呼び出しは **gate 呼び出しより前**にあるので、
    `GateOutcome.resolved` は実 resolver の産物のままであり、
    `indicator_unresolved` の判別子・`indicator_deps` payload・P3' の
    `is` 同一性はすべて実経路で観測できる。

    **使ってはいけないところ**: 実 backtest そのものが受入の対象である
    A1 / A1-b / C1 (`tests/plugin/test_indicator_wiring_e2e.py`) は実 worker で
    回す (Global Constraints「モックで実 worker を潰さない」)。

    **verdict の形は既存の動作実績のある double からの逐語転写**
    ([[test-fixtures-from-real-transcripts]]): `tests/plugin/
    test_approval_payload_common_contract.py:90-100` の `_fake_pytest_ok` /
    `_fake_evaluable_gate` (着手時に
    `rg -n '_fake_evaluable_gate|_fake_pytest_ok' -A 8
    tests/plugin/test_approval_payload_common_contract.py` で再取得)。
    **`cpu_samples` は載せない** — このフィールドは T4a で追加されるので、
    T6b (= T4a より前にマージする task) の smoke test 時点では
    `TypeError` になる。T4a 以降の呼び出し元で cpu_samples が必要な
    テストは、その場で `dataclasses.replace(verdict, cpu_samples=...)`
    するか実 gate を使うこと。

    `pytest_ok=True` (既定) は `switch.run_gate_pytest` も差し替える —
    候補ごとの実 pytest 実行は本 task の観測対象ではなく、上記の既存
    テストも同じ組で差し替えている。

    戻り値 = gate が受け取った kwargs を積む list。
    """
    from agentic_fx.plugin import strategy_gate
    from agentic_fx.plugin.gate_pytest import GateResult
    seen: list[dict] = []

    def _fake(conn, **kw):
        seen.append(kw)
        return strategy_gate.StrategyGateVerdict(
            evaluable=True, baseline_variant="no_strategy",
            baseline_row={"plugin_ref": "no_strategy:cand",
                          "variant": "no_strategy"},
            candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.2}})

    monkeypatch.setattr(
        strategy_gate, "evaluate_strategy_adoption_gate", _fake)
    # `approval.py` は `from agentic_fx.plugin import strategy_gate` で
    # **モジュールを** import しているので (`rg -n 'import strategy_gate'
    # src/agentic_fx/plugin/approval.py` で着手時に再確認)、モジュール属性の
    # 差し替えで call site を捕捉できる。関数を直接 import する形に変わって
    # いたらここも `approval.evaluate_strategy_adoption_gate` に変えること
    # ([[shared-reader-crosses-layer-boundaries]])。
    if pytest_ok:
        from agentic_fx.plugin import switch as _switch
        monkeypatch.setattr(
            _switch, "run_gate_pytest",
            lambda plugin_dir, *, settings: GateResult(
                passed=True, returncode=0, stdout_tail="1 passed",
                duration_sec=0.1))
    return seen

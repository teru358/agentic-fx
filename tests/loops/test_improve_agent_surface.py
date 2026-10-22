"""改善 agent が見る面の遮断と固定語彙 (prompt / 履歴 / activity / 降格の通知)。

面ごとに別の canary 値を DB に置き、入力側に実在すること (positive control) を
先に確認してから、その面に現れないことを確認する。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.config import load_settings
from agentic_fx.loops import improve_context
from agentic_fx.loops.improve_context import (
    DUPLICATE_LAST_RESULT, NOT_SUBMITTED_LAST_RESULT, build_improve_context,
    family_redirect_last_result, project_last_result)
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
from agentic_fx.store import approvals
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import candidate_archives as archives
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store
from agentic_fx.store.db import connect, init_db

from tests.loops.test_improve_candidate_ledger import (
    NOW, _fake_holdout_metrics, _fake_in_sample_metrics, _plugin_artifact,
    _run, _strategy, improve_env)
from tests.loops.test_improve_loop_duplicate_metrics import (
    _CONVERGED_METRICS, _HOLDOUT_METRICS, _STRATEGY_PY_VARIANT_A,
    _STRATEGY_PY_VARIANT_B, _run_strategy_mission)

_EXAMPLE = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _strict_settings():
    """床の 3 値を一組で厳しくした settings (holdout の評価可否を要求する)。"""
    gate = _EXAMPLE.improve.gate.model_copy(update={
        "min_pf": 1.0, "require_positive_avg_r": True,
        "require_holdout_evaluable": True})
    improve = _EXAMPLE.improve.model_copy(update={"gate": gate})
    return _EXAMPLE.model_copy(update={"improve": improve})


class _Ctx:
    def __init__(self, tmp_path):
        self.staging_dir = tmp_path / "staging"
        self.source_snapshot_dir = tmp_path / "source"
        self.inventory_view = {"plugins": [], "pin_broken_strategies": []}


def _render(conn, settings, tmp_path) -> tuple[str, dict]:
    ctx_data = build_improve_context(
        conn, settings=settings, now=NOW, root=tmp_path,
        allowed_backlog_ids=None,
        inventory_view={"plugins": [], "pin_broken_strategies": []})
    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = settings
    return loop._render_improve_mission_prompt(
        ctx_data, ctx=_Ctx(tmp_path)), ctx_data


def _plant_ledger_and_decision(conn, *, tag: str) -> dict[str, str]:
    """人間の却下 (reason 付き)、他候補の成績・hash を持つ台帳の行、
    holdout の数値を持つ承認 payload を実フローで作る。canary を返す。"""
    canary = {
        "reason": f"reasoncanary_{tag}_a41c",
        "holdout_pf": {"p": "0.7351", "h": "0.7352", "a": "0.7353",
                       "d": "0.7354"}[tag],
        "other_pf": {"p": "0.6283", "h": "0.6284", "a": "0.6285",
                     "d": "0.6286"}[tag],
        "artifact_hash": f"arthashcanary_{tag}_9d2e",
        "content_hash": f"conthashcanary_{tag}_77b0",
        "logic_hash": f"logichashcanary_{tag}_c3f5",
        "name": f"namecanary_{tag}_e60a",
    }
    backlog_id = backlog_store.add(conn, f"idea {tag}", "user", NOW)
    backlog_store.select_for_mission(conn, backlog_id, now=NOW)
    improve_runs_store.start(conn, backlog_id, NOW)
    approval_id = approvals.create(
        conn, kind="plugin", now=NOW, payload={
            "backlog_id": backlog_id, "name": canary["name"],
            "holdout": {"pf": float(canary["holdout_pf"]), "trades": 4}})
    approvals.apply_decision(
        conn, approval_id, "rejected", decided_by="shell", now=NOW,
        reason=canary["reason"])
    mission_id = missions_store.start(
        conn, "improve", "codex", "gpt-5", now=NOW, commit=True)
    archives.insert(
        conn, mission_id=mission_id, name=canary["name"],
        content_hash=canary["content_hash"],
        artifact_hash=canary["artifact_hash"], archive_path=None,
        pair="USDJPY", metrics={"pf": float(canary["other_pf"]), "trades": 41},
        now=NOW, logic_hash=canary["logic_hash"], commit=True)
    return canary


def _assert_planted(conn, canary: dict[str, str]) -> None:
    """入力側に canary が実在する (positive control)。"""
    reason = conn.execute(
        "SELECT reason FROM approval_requests WHERE reason=?",
        (canary["reason"],)).fetchone()
    assert reason is not None
    payload = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE payload_json LIKE ?",
        (f"%{canary['holdout_pf']}%",)).fetchone()
    assert payload is not None
    row = conn.execute(
        "SELECT metrics_json, artifact_hash, content_hash, logic_hash "
        "FROM candidate_archives WHERE artifact_hash=?",
        (canary["artifact_hash"],)).fetchone()
    assert row is not None
    assert canary["other_pf"] in row["metrics_json"]
    assert row["content_hash"] == canary["content_hash"]
    assert row["logic_hash"] == canary["logic_hash"]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "surface.db")
    init_db(c)
    yield c
    c.close()


_HOLDOUT_WORDS = ("標本不足", "評価可能", "30 trades", "holdout_not_evaluable")


# --- AC-13a: prompt -------------------------------------------------------

def test_ac13a_prompt_has_none_of_the_internal_canaries(conn, tmp_path):
    canary = _plant_ledger_and_decision(conn, tag="p")
    _assert_planted(conn, canary)

    rendered, ctx_data = _render(conn, _strict_settings(), tmp_path)

    # 履歴を実際に通っている (却下の固定文言は出る)
    assert "rejected_by_human" in rendered
    for value in canary.values():
        assert value not in rendered, value
        assert value not in json.dumps(ctx_data, default=str), value
    for word in _HOLDOUT_WORDS:
        assert word not in rendered, word


# --- AC-13b: 履歴の節 -------------------------------------------------------

def test_ac13b_prompt_has_no_candidate_history_section(conn, tmp_path):
    canary = _plant_ledger_and_decision(conn, tag="h")
    _assert_planted(conn, canary)

    rendered, _ = _render(conn, _strict_settings(), tmp_path)

    headings = [l for l in rendered.splitlines() if l.startswith("#")]
    for fragment in ("試した仮説", "候補の履歴", "候補台帳"):
        assert not [h for h in headings if fragment in h]
    for value in canary.values():
        assert value not in rendered, value


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names)
    return found


def test_ac13b_improve_context_does_not_import_candidate_archives():
    imported = _imported_modules(Path(improve_context.__file__))
    assert not [m for m in imported if "candidate_archives" in m]


# --- AC-13c: activity の agent 投影 ------------------------------------------

_IMPROVE_TOOL_NAMES = {
    # research
    "web_search", "fetch_article",
    # staging
    "list_staging", "read_staging_file", "write_staging_file",
    "read_plugin_source", "list_examples", "read_example_plugin",
    "run_plugin_tests", "list_deployed_plugins", "lock_staging_deps",
    # rpc
    "run_backtest", "analyze_corr",
}


def test_ac13c_improve_registry_tool_names_are_the_allowlist(improve_env):
    from datetime import datetime, timezone
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.tools.mission_registry import build_mission_registry

    app, root = improve_env
    staging = root / "staging_probe"
    source = root / "source_probe"
    staging.mkdir()
    source.mkdir()
    registry = build_mission_registry(
        "improve", app.conn_core, app.settings,
        FixedClock(datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)),
        app.rag, activity=app.activity, staging_dir=staging,
        source_snapshot_dir=source,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {},
                      "analyze_corr": lambda a: {}})
    assert set(registry.names()) == _IMPROVE_TOOL_NAMES
    assert not [n for n in registry.names() if "activity" in n]


def test_ac13c_improve_context_does_not_import_activity():
    imported = _imported_modules(Path(improve_context.__file__))
    assert not [m for m in imported
                if m == "agentic_fx.activity" or m.endswith(".activity")
                or m.startswith("agentic_fx.activity.")]


def test_ac13c_activity_canary_stays_out_of_the_prompt(improve_env, tmp_path):
    app, root = improve_env
    canary = "activitycanary_a_5f0b19"
    app.activity.write(
        __import__("agentic_fx.activity", fromlist=["Category"]).Category.IMPROVE,
        "quality_demoted", f"mission=1 content_hash={canary} pair=USDJPY")
    assert canary in "\n".join(app.activity.tail(10_000))  # positive control

    rendered, ctx_data = _render(app.conn_core, _strict_settings(), root)

    assert canary not in rendered
    assert canary not in json.dumps(ctx_data, default=str)


# --- AC-13d: 降格の通知 ------------------------------------------------------

_ALLOWED_LAST_RESULTS = {
    DUPLICATE_LAST_RESULT, NOT_SUBMITTED_LAST_RESULT}


def test_ac13d_duplicate_metrics_demotion_writes_fixed_last_result(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _run_strategy_mission(
        app, root, name="notice_a", plugin_py=_STRATEGY_PY_VARIANT_A,
        in_sample_metrics=_CONVERGED_METRICS,
        holdout_metrics=_HOLDOUT_METRICS)
    content_hash_a = conn.execute(
        "SELECT content_hash FROM backtest_runs WHERE scope='in_sample' "
        "AND variant='candidate' ORDER BY id LIMIT 1").fetchone()[0]

    ctx_b = _run_strategy_mission(
        app, root, name="notice_b", plugin_py=_STRATEGY_PY_VARIANT_B,
        in_sample_metrics=_CONVERGED_METRICS,
        holdout_metrics=_HOLDOUT_METRICS)

    backlog_id = conn.execute(
        "SELECT backlog_id FROM improvement_runs WHERE id=?",
        (ctx_b.run_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=? "
        "AND last_result IS NOT NULL", (backlog_id,)).fetchall()
    assert len(rows) == 1  # 通知が 1 件生成された
    last_result = rows[0][0]
    assert last_result in _ALLOWED_LAST_RESULTS
    assert last_result == DUPLICATE_LAST_RESULT
    for leak in (content_hash_a, "USDJPY", "1.4981", "194", "pair=",
                 "duplicate_metrics_of"):
        assert leak not in last_result

    activity = "\n".join(app.activity.tail(10_000))
    demoted = [l for l in activity.splitlines() if "quality_demoted" in l]
    assert len(demoted) == 1
    assert content_hash_a in demoted[0] and "pair=USDJPY" in demoted[0]


def test_ac13d_fixed_vocabulary_covers_every_agent_facing_demotion():
    """重複 (ロジック等価・成績完全一致) と同族への誘導は、固定の写像だけで
    作る。他候補の hash・pair・成績は引数にも出力にも入らない。"""
    assert DUPLICATE_LAST_RESULT == "observation:duplicate"
    assert NOT_SUBMITTED_LAST_RESULT == "observation:not_submitted"
    assert (family_redirect_last_result("sma_cross")
            == "observation:family_redirect:sma_cross")


@pytest.mark.parametrize("legacy,expected", [
    ("unprofitable", NOT_SUBMITTED_LAST_RESULT),
    ("insufficient_trades:12", NOT_SUBMITTED_LAST_RESULT),
    ("history_source_unconfigured", NOT_SUBMITTED_LAST_RESULT),
    ("gate_failed:backtest_data_unavailable:no bars", NOT_SUBMITTED_LAST_RESULT),
    ("observation:duplicate_metrics_of:abc123 pair=USDJPY",
     DUPLICATE_LAST_RESULT),
    ("rejected_by_human", "rejected_by_human"),
    ("gate_failed:noop_copy_of:x", "gate_failed:noop_copy"),
    (None, None),
])
def test_ac13d_legacy_last_results_are_projected_on_the_way_out(
        legacy, expected):
    assert project_last_result(legacy) == expected


def test_ac13d_legacy_values_in_the_history_table_are_projected(
        conn, tmp_path):
    """移行前に保存された値が、履歴の節に区別つきで出ない。"""
    leaked = {"unprofitable": 1, "insufficient_trades:12": 2,
              "observation:duplicate_metrics_of:legacyhash99 pair=USDJPY": 3}
    for value, i in leaked.items():
        backlog_id = backlog_store.add(conn, f"legacy idea {i}", "user", NOW)
        backlog_store.set_status(conn, backlog_id, "observation", NOW,
                                 last_result=value)
        improve_runs_store.start(conn, backlog_id, NOW)

    rendered, _ = _render(conn, _strict_settings(), tmp_path)

    assert "legacyhash99" not in rendered
    assert "insufficient_trades" not in rendered
    assert "unprofitable" not in rendered
    assert rendered.count(NOT_SUBMITTED_LAST_RESULT) >= 2
    assert DUPLICATE_LAST_RESULT in rendered


# --- AC-24: 3 状態の表示が同じ -------------------------------------------------

def _terminate(app, root, name, *, kind):
    """`kind` の非提出で mission を終端させ、(backlog の last_result,
    その mission が起票した note の origin_outcome) を返す。"""
    output = _plugin_artifact(name, kind="strategy")
    output["discoveries"] = [
        {"idea": f"note for {name}", "source": "agent", "evidence": "e",
         "kind": "fact"}]
    kwargs = {"files": _strategy(name)}
    if kind == "unprofitable":
        kwargs["in_sample"] = _fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1)
        kwargs["holdout_fn"] = _fake_holdout_metrics()
    elif kind == "insufficient_trades":
        kwargs["in_sample"] = _fake_in_sample_metrics(29)
    else:
        verdict = StrategyGateVerdict(
            evaluable=False, observation_reason="history_source_unconfigured",
            unevaluable_kind="history_source_unconfigured")
        kwargs["extra"] = [patch.object(
            ImproveLoop, "_run_strategy_gate", lambda self, *a, **k: verdict)]
    loop, ctx, _seeded = _run(app, root, output, **kwargs)
    conn = app.conn_core
    backlog_id = conn.execute(
        "SELECT backlog_id FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()[0]
    last_result = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()[0]
    origin = conn.execute(
        "SELECT origin_outcome FROM improvement_backlog WHERE idea=?",
        (f"note for {name}",)).fetchone()[0]
    return backlog_id, last_result, origin


def test_ac24_three_internal_states_look_identical_to_the_agent(
        improve_env):
    app, root = improve_env
    states = ("unprofitable", "insufficient_trades", "comparison_unavailable")
    seen = {s: _terminate(app, root, f"st_{i}", kind=s)
            for i, s in enumerate(states)}

    last_results = {s: seen[s][1] for s in states}
    origins = {s: seen[s][2] for s in states}
    assert set(last_results.values()) == {NOT_SUBMITTED_LAST_RESULT}
    assert set(origins.values()) == {"not_submitted"}

    # 履歴の節と origin 列の描画も 3 状態で差が無い
    rendered, _ = _render(app.conn_core, _strict_settings(), root)
    for word in ("unprofitable", "insufficient_trades",
                 "history_source_unconfigured"):
        assert word not in rendered, word
    history_rows = [
        line for line in rendered.splitlines()
        if line.rstrip().endswith(f"| {NOT_SUBMITTED_LAST_RESULT} |")]
    assert len(history_rows) == 3
    origin_cells = [
        line for line in rendered.splitlines()
        if line.startswith("| ") and line.rstrip().endswith("| not_submitted |")]
    # 各 mission が起票した 2 行 (選んだ課題と note) が同じ語になる
    assert len(origin_cells) == 6


def test_ac24_unprofitable_and_insufficient_both_write_not_submitted_origin(
        improve_env):
    app, root = improve_env
    _, _, origin_u = _terminate(app, root, "origin_u", kind="unprofitable")
    _, _, origin_i = _terminate(app, root, "origin_i",
                                kind="insufficient_trades")
    assert origin_u == origin_i == "not_submitted"
    stored = {r[0] for r in app.conn_core.execute(
        "SELECT DISTINCT origin_outcome FROM improvement_backlog "
        "WHERE origin_outcome IS NOT NULL")}
    assert stored == {"not_submitted"}


def test_backlog_last_result_canaries_are_absent_from_context_and_prompt(
        conn, tmp_path):
    values = {"gate_failed:holdout_pf=0.7391 trades=4242": "open_like",
              "insufficient_trades:7777": "note_like"}
    for i, value in enumerate(values):
        backlog_id = backlog_store.add(conn, f"canary backlog {i}", "user", NOW)
        backlog_store.set_status(
            conn, backlog_id, "observation" if i == 0 else "note", NOW,
            last_result=value)
    stored = " ".join(r["last_result"] for r in conn.execute(
        "SELECT last_result FROM improvement_backlog"))
    assert "0.7391" in stored and "7777" in stored

    rendered, ctx_data = _render(conn, _strict_settings(), tmp_path)

    for text in (json.dumps(ctx_data), rendered):
        for token in ("0.7391", "4242", "7777"):
            assert token not in text


_DYNAMIC_SUFFIX_VALUES = (
    ("approval_pending:4719", "approval_pending", "4719"),
    ("observation:family_redirect:Bad-Name",
     "observation:family_redirect", "Bad-Name"),
    ("gate_failed:noop_copy_of:" + "ab12" * 10, "gate_failed:noop_copy",
     "ab12ab12"),
    ("gate_failed:noop_copy_of:plugincanary_rsi_pullback",
     "gate_failed:noop_copy", "plugincanary"),
)


def test_dynamic_suffixes_of_last_result_never_reach_context_or_prompt(
        conn, tmp_path):
    """承認番号・候補名・hash の suffix は、投影後の文脈にも prompt にも残らない。"""
    for i, (value, _, _) in enumerate(_DYNAMIC_SUFFIX_VALUES):
        backlog_id = backlog_store.add(conn, f"dyn idea {i}", "user", NOW)
        backlog_store.set_status(conn, backlog_id, "observation", NOW,
                                 last_result=value)
        improve_runs_store.start(conn, backlog_id, NOW)
    stored = [r[0] for r in conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id")]
    assert stored == [v for v, _, _ in _DYNAMIC_SUFFIX_VALUES]

    rendered, ctx_data = _render(conn, _strict_settings(), tmp_path)
    blob = json.dumps(ctx_data, default=str, ensure_ascii=False)
    for _, folded, needle in _DYNAMIC_SUFFIX_VALUES:
        assert folded in blob and folded in rendered
        assert needle not in blob, needle
        assert needle not in rendered, needle
    assert "4719" not in blob and "4719" not in rendered

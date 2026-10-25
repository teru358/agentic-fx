"""終端 snapshot の (result_code, reason_code) を、実際の終端経路で確かめる。

足場は `test_improve_candidate_ledger` / `test_improve_quality_gate` の駆動
ヘルパ (prepare → staging 配置 → commit) を再利用する。"""
from __future__ import annotations

import dataclasses
import re
import sqlite3
from unittest.mock import patch

import pytest

from agentic_fx.backtest import holdout
from agentic_fx.loops import improve_notice_codes as codes
from agentic_fx.loops.improve_loop import ImproveLoop, ImprovePrepareFailed
from agentic_fx.plugin import loader as plugin_loader
from agentic_fx.plugin.gate_pytest import GateResult
from agentic_fx.plugin.resolve import IndicatorResolutionError
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_waves

from tests.loops.test_improve_candidate_ledger import (
    _PASSING_INDICATOR_CONFIG,
    _PASSING_INDICATOR_PY,
    _PASSING_INDICATOR_TEST,
    _REWRITTEN,
    _indicator,
    _loop,
    _plugin_artifact,
    _report_artifact,
    _rewriting_gate,
    _run,
    _strategy,
    _supervisor,
)
from tests.loops.test_improve_e2e import (
    NOW,
    FakeImproveWorkerRunner,
    _fake_holdout_metrics,
    _fake_in_sample_metrics,
    improve_env,  # noqa: F401
)
from tests.loops.test_improve_quality_gate import (
    FAMILY,
    METRICS_1,
    METRICS_2,
    PY_X,
    PY_Y,
    _decided_row,
    _deployed,
    _family_patch,
    _hash_of,
    _identity_patch,
    _population_patch,
    _run_strategy,
)

TOKEN = "https://leak.example/path?apikey=SECRET9"
_FIXED_WORD = re.compile(r"^[a-z_]+$")


def _snap(conn, mission_id):
    row = conn.execute("SELECT * FROM improve_run_notices WHERE mission_id=?",
                       (mission_id,)).fetchone()
    assert row is not None, f"no snapshot for mission {mission_id}"
    assert row["reason_code"] is None or _FIXED_WORD.match(row["reason_code"])
    codes.validate_pair(row["result_code"], row["reason_code"])
    return row


def _pair(conn, mission_id):
    row = _snap(conn, mission_id)
    return row["result_code"], row["reason_code"]


def _all_text(row) -> str:
    return " ".join(str(v) for v in dict(row).values())


def _report_symlink(root):
    def plant(loop, ctx):
        tmp_dir = root / "data" / "improve_reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / "evil.md"
        target.write_text("x", encoding="utf-8")
        (tmp_dir / f"improve-{ctx.mission_id}.md.part").symlink_to(target)
    return plant


def _observation_output(reason):
    return {"discoveries": [], "selected": {"backlog_id": None, "idea": "an idea"},
            "artifact": {"type": "observation", "reason": reason},
            "selection_rationale": "r"}


# --- 提出 / 観察 / 報告 -----------------------------------------------------

def test_submitted(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        result_kwargs={"tool_calls": 5})
    row = _snap(conn, ctx.mission_id)
    approval_id = conn.execute("SELECT id FROM approval_requests").fetchone()[0]
    assert (row["trigger"], row["result_code"], row["reason_code"],
            row["tool_calls"], row["observation_text"], row["approval_id"]) \
        == ("manual", "submitted", None, 5, None, approval_id)
    assert conn.execute("SELECT trigger FROM improvement_runs WHERE id=?",
                        (ctx.run_id,)).fetchone()[0] == "manual"


def test_observation_keeps_the_agents_own_words(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output("nothing found"),
                        result_kwargs={"tool_calls": 0})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"],
            row["observation_text"]) == ("observation", None, 0, "nothing found")


def test_observation_text_is_scrubbed_and_cut_to_the_limit(improve_env):
    app, root = improve_env
    conn = app.conn_core
    long_reason = "x" * 2500
    loop, ctx, _ = _run(app, root, _observation_output(
        f"saw {TOKEN} and then {long_reason}"))
    row = _snap(conn, ctx.mission_id)
    assert "leak.example" not in row["observation_text"]
    assert "SECRET9" not in row["observation_text"]
    assert len(row["observation_text"]) == 2000


@pytest.mark.parametrize("size", [1999, 2000, 2001])
def test_observation_text_boundary_keeps_the_mission_terminal(improve_env, size):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output("y" * size))
    row = _snap(conn, ctx.mission_id)
    assert len(row["observation_text"]) == min(size, 2000)
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("reason", [
    "duplicate of something", "duplicate",
    "family_redirect:abc_123",
    f"duplicate {TOKEN} 7.7777",
    f"family_redirect:{TOKEN} 12345"])
def test_observation_masked_by_the_selector_has_no_text(improve_env, reason):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output(reason),
                        result_kwargs={"tool_calls": 2})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["observation_text"]) \
        == ("observation", "not_submitted", None)
    assert "SECRET9" not in _all_text(row) and "7.7777" not in _all_text(row)
    last = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert last == "observation:not_submitted"


@pytest.mark.parametrize("reason", ["dupl", "not a duplicate", "Duplicate",
                                    "family_redirect", " duplicate"])
def test_observation_that_only_resembles_the_prefix_is_kept(improve_env, reason):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output(reason))
    row = _snap(conn, ctx.mission_id)
    assert (row["reason_code"], row["observation_text"]) == (None, reason)


def test_selector_decides_last_result_text_and_notice_together():
    from agentic_fx.loops.improve_context import NOT_SUBMITTED_LAST_RESULT
    from agentic_fx.loops.improve_loop import _select_observation
    for reason in ("duplicate of x", "family_redirect:abc", "duplicate"):
        masked = _select_observation({"type": "observation", "reason": reason})
        assert masked.notice == codes.NOTICE_OBSERVATION_NOT_SUBMITTED
        assert masked.observation_text is None
        assert masked.last_result == NOT_SUBMITTED_LAST_RESULT
    kept = _select_observation(
        {"type": "observation", "reason": f"saw {TOKEN} twice"})
    assert kept.notice == codes.NOTICE_OBSERVATION
    assert kept.observation_text == kept.last_result.removeprefix("observation:")
    assert "SECRET9" not in kept.observation_text
    assert "leak.example" not in kept.observation_text


def test_recovered_report_is_demoted(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("core"),
                        result_kwargs={"recovered": True, "tool_calls": 3})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["observation_text"],
            row["tool_calls"]) == ("observation", "demoted_recovered", None, 3)


def test_risk_gate_report_is_unsupported(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("risk_gate"))
    assert _pair(conn, ctx.mission_id) == ("observation", "risk_gate_unsupported")


def test_report_published(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("core"),
                        result_kwargs={"tool_calls": 4})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("report", None, 4)


def test_loser_publishes_a_duplicate_skipped_report(improve_env):
    app, root = improve_env
    conn = app.conn_core
    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)
    output = _plugin_artifact("cand")
    output["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}

    def take(loop, ctx):
        conn.execute("UPDATE improvement_backlog SET status='selected' "
                     "WHERE id=?", (backlog_id,))
        conn.commit()

    loop, ctx, _ = _run(app, root, output, files=_indicator(),
                        before_commit=take, result_kwargs={"tool_calls": 6})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("report", "duplicate_skipped", 6)


def test_report_write_failure_in_the_report_branch(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("core"),
                        before_commit=_report_symlink(root))
    assert _pair(conn, ctx.mission_id) == ("failed", "report_write_failed")
    # mission 自体は completed のまま (取引は止めない)
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"


def test_report_write_failure_in_the_loser_branch(improve_env):
    app, root = improve_env
    conn = app.conn_core
    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)
    output = _plugin_artifact("cand")
    output["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}
    plant = _report_symlink(root)

    def take_and_plant(loop, ctx):
        conn.execute("UPDATE improvement_backlog SET status='selected' "
                     "WHERE id=?", (backlog_id,))
        conn.commit()
        plant(loop, ctx)

    loop, ctx, _ = _run(app, root, output, files=_indicator(),
                        before_commit=take_and_plant)
    assert _pair(conn, ctx.mission_id) == ("failed", "report_write_failed")
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"


def test_gate_report_write_failure_keeps_the_gate_word(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(29),
        before_commit=_report_symlink(root))
    assert _pair(conn, ctx.mission_id) == ("gate_failed", "insufficient_trades")


# --- 質検査の降格・拒否 (4 種すべて実経路) ------------------------------------

def test_quality_duplicate_logic(improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash=_hash_of(PY_X),
                 outcome="rejected_by_human")
    loop, ctx, _ = _run_strategy(app, root, "cand", metrics=METRICS_2)
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["observation_text"]) \
        == ("observation", "duplicate_logic", None)


def test_quality_duplicate_metrics(improve_env):
    app, root = improve_env
    conn = app.conn_core
    first, ctx_a, _ = _run_strategy(app, root, "conv_a", py=PY_X)
    assert _pair(conn, ctx_a.mission_id) == ("submitted", None)
    loop, ctx_b, _ = _run_strategy(app, root, "conv_b", py=PY_Y)
    assert _pair(conn, ctx_b.mission_id) == ("observation", "duplicate_metrics")


def test_quality_family_redirect(improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("alpha", family=FAMILY)
    loop, ctx, _ = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed]), _family_patch(FAMILY)])
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["observation_text"]) \
        == ("observation", "family_redirect", None)
    assert "alpha" not in _all_text(row)


def _raise_value_error(*a, **k):
    raise ValueError("archive unreadable")


def test_quality_comparison_base_unavailable(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run_strategy(
        app, root, "cand",
        extra=[patch("agentic_fx.loops.improve_quality.load_archive",
                     _raise_value_error)])
    assert _pair(conn, ctx.mission_id) == ("gate_failed",
                                           "comparison_base_unavailable")


def test_quality_deploy_guard_unavailable_is_the_same_word(improve_env):
    from agentic_fx.plugin import switch as plugin_switch
    app, root = improve_env
    conn = app.conn_core

    def busy(*a, **k):
        raise plugin_switch.PluginBusyError("busy")

    loop, ctx, _ = _run_strategy(
        app, root, "cand",
        extra=[patch.object(plugin_switch, "deploy_guard", busy)])
    assert _pair(conn, ctx.mission_id) == ("gate_failed",
                                           "comparison_base_unavailable")


# --- 評価の結果 ------------------------------------------------------------

def test_unprofitable(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        in_sample=_fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1),
        holdout_fn=_fake_holdout_metrics(), result_kwargs={"tool_calls": 8})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("gate_failed", "unprofitable", 8)
    assert not re.search(r"\d\.\d", _all_text(row).replace(
        row["created_at"], ""))


def test_insufficient_trades_carries_no_number(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(16))
    row = _snap(conn, ctx.mission_id)
    assert row["reason_code"] == "insufficient_trades"
    last = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert last == "observation:not_submitted"


def test_history_source_unconfigured(improve_env):
    from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
    app, root = improve_env
    conn = app.conn_core
    verdict = StrategyGateVerdict(
        evaluable=False, observation_reason="history_source_unconfigured",
        unevaluable_kind="history_source_unconfigured")
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        extra=[patch.object(ImproveLoop, "_run_strategy_gate",
                            lambda self, *a, **k: verdict)])
    assert _pair(conn, ctx.mission_id) == ("gate_failed",
                                           "history_source_unconfigured")


def test_backtest_data_unavailable(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def no_history(settings, *, history_conn, record_fn, **kwargs):
        raise holdout.NoHistoryError(f"no bars {TOKEN}")

    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=no_history)
    row = _snap(conn, ctx.mission_id)
    assert row["reason_code"] == "backtest_data_unavailable"
    assert "SECRET9" not in _all_text(row)


# --- plugin gate (実 commit。gate 内部の分岐は固定語を直接検査) --------------

def _gate_case(improve_env, *, files=None, extra=(), before_commit=None,
               kind="indicator"):
    app, root = improve_env
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind=kind),
        files=files or (_indicator() if kind == "indicator" else _strategy()),
        extra=extra, before_commit=before_commit)
    return _pair(app.conn_core, ctx.mission_id)


def test_gate_snapshot_rejected(improve_env):
    def grow(loop, ctx):
        (ctx.staging_dir / "cand" / "nested").mkdir()

    assert _gate_case(improve_env, before_commit=grow) \
        == ("gate_failed", "snapshot_rejected")


def test_gate_source_rejected(improve_env):
    files = ("cand", "import subprocess\n" + _PASSING_INDICATOR_PY,
             _PASSING_INDICATOR_CONFIG, _PASSING_INDICATOR_TEST)
    assert _gate_case(improve_env, files=files) \
        == ("gate_failed", "source_rejected")


def test_gate_loader_rejected_inside_the_gate(improve_env):
    files = ("cand", _PASSING_INDICATOR_PY, "not: a plugin config\n",
             _PASSING_INDICATOR_TEST)
    assert _gate_case(improve_env, files=files) \
        == ("gate_failed", "loader_rejected")


def _stateful_discover(second):
    original = plugin_loader._discover_one
    calls = []

    def wrapped(entry, name):
        calls.append(1)
        meta = original(entry, name)
        return meta if len(calls) == 1 else second(meta)

    return patch.object(plugin_loader, "_discover_one", wrapped)


def test_gate_loader_rejected_after_the_gate(improve_env):
    assert _gate_case(improve_env, extra=[_stateful_discover(lambda m: None)]) \
        == ("gate_failed", "loader_rejected")


def test_gate_syntax_error(improve_env):
    def boom(*a, **k):
        raise SyntaxError("bad")

    assert _gate_case(improve_env, extra=[
        patch("agentic_fx.loops.improve_loop.find_noop_copy", boom)]) \
        == ("gate_failed", "syntax_error")


def test_gate_noop_copy(improve_env):
    assert _gate_case(improve_env, extra=[
        patch("agentic_fx.loops.improve_loop.find_noop_copy",
              lambda *a, **k: "_examples/rsi")]) == ("gate_failed", "noop_copy")


def test_gate_self_test_missing(improve_env):
    files = ("cand", _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
             "x = 1\n")
    assert _gate_case(improve_env, files=files) \
        == ("gate_failed", "self_test_missing")


def test_gate_pytest_failed(improve_env):
    assert _gate_case(improve_env, extra=[
        patch("agentic_fx.loops.improve_loop.run_gate_pytest",
              lambda d, *, settings: GateResult(False, 1, f"boom {TOKEN}", 0.0))]
    ) == ("gate_failed", "pytest_failed")


def test_gate_self_test_too_thin(improve_env):
    assert _gate_case(improve_env, extra=[
        patch("agentic_fx.loops.improve_loop.run_gate_pytest",
              lambda d, *, settings: GateResult(True, 0, "1 passed in 0.1s", 0.0))]
    ) == ("gate_failed", "self_test_too_thin")


def test_gate_self_test_mutated(improve_env):
    assert _gate_case(improve_env, extra=[
        patch("agentic_fx.loops.improve_loop.run_gate_pytest",
              _rewriting_gate(_REWRITTEN, passed=True,
                              stdout="99 passed in 0.1s"))]) \
        == ("gate_failed", "self_test_mutated")


def test_gate_sandbox_unavailable_is_failed_but_the_mission_completes(improve_env):
    def unavailable(d, *, settings):
        raise RuntimeError("landlock unavailable")

    app, root = improve_env
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch("agentic_fx.loops.improve_loop.run_gate_pytest",
                     unavailable)])
    assert _pair(app.conn_core, ctx.mission_id) == ("failed", "sandbox_unavailable")
    assert app.conn_core.execute("SELECT status FROM missions WHERE id=?",
                                 (ctx.mission_id,)).fetchone()[0] == "completed"


def test_gate_unverified(improve_env):
    assert _gate_case(improve_env, kind="strategy",
                      extra=[_identity_patch(logic_hash=None)]) \
        == ("gate_failed", "unverified")


def test_gate_max_bars_limit_carries_no_message(improve_env):
    app, root = improve_env
    config = _strategy()[2].replace("params: {}", "max_bars: 999999\nparams: {}")
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=("cand", _strategy()[1], config, _strategy()[3]))
    row = _snap(app.conn_core, ctx.mission_id)
    assert row["reason_code"] == "max_bars_limit"
    assert "999999" not in _all_text(row)


def test_gate_kind_unsupported(improve_env):
    assert _gate_case(improve_env, extra=[
        _stateful_discover(lambda m: dataclasses.replace(m, kind="weird")),
        patch("agentic_fx.plugin.approval.assert_max_bars_within_limit",
              lambda *a, **k: None)]) == ("gate_failed", "kind_unsupported")


def test_gate_outputs_required(improve_env):
    assert _gate_case(improve_env, extra=[
        patch("agentic_fx.plugin.approval.outputs_required_violation",
              lambda meta: True)]) == ("gate_failed", "outputs_required")


def test_gate_indicator_unresolved(improve_env):
    def unresolved(*a, **k):
        raise IndicatorResolutionError("rsi", f"missing {TOKEN}")

    app, root = improve_env
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        extra=[patch("agentic_fx.plugin.resolve.resolve_indicator_deps",
                     unresolved)])
    row = _snap(app.conn_core, ctx.mission_id)
    assert row["reason_code"] == "indicator_unresolved"
    assert "SECRET9" not in _all_text(row) and "rsi" not in row["reason_code"]


def _syntax_error(*a, **k):
    raise SyntaxError("bad")


def _gate_patch(name, value):
    return patch(f"agentic_fx.loops.improve_loop.{name}", value)


def _pytest_runtime_error(plugin_dir, *, settings):
    raise RuntimeError("landlock unavailable")


GATE_CASES = {
    "source_rejected": dict(plugin_py="import subprocess\n"),
    "syntax_error": dict(patches=[_gate_patch("find_noop_copy", _syntax_error)]),
    "noop_copy": dict(patches=[_gate_patch(
        "find_noop_copy", lambda *a, **k: "_examples/x")]),
    "self_test_missing": dict(test_py="x = 1\n"),
    "pytest_failed": dict(patches=[_gate_patch(
        "run_gate_pytest",
        lambda d, *, settings: GateResult(False, 1, "boom", 0.0))]),
    "self_test_too_thin": dict(patches=[_gate_patch(
        "run_gate_pytest",
        lambda d, *, settings: GateResult(True, 0, "1 passed in 0.1s", 0.0))]),
    "self_test_mutated": dict(patches=[_gate_patch(
        "run_gate_pytest",
        _rewriting_gate(_REWRITTEN, passed=True, stdout="99 passed in 0.1s"))]),
    "sandbox_unavailable": dict(patches=[_gate_patch(
        "run_gate_pytest", _pytest_runtime_error)]),
}


@pytest.mark.parametrize("expected", sorted(GATE_CASES))
def test_plugin_gate_verdict_carries_the_fixed_code(improve_env, tmp_path, expected):
    from tests.loops.test_improve_candidate_ledger import _gate_dir
    app, root = improve_env
    loop = _loop(app, root)
    case = GATE_CASES[expected]
    kw = {k: case[k] for k in ("plugin_py", "test_py") if k in case}
    directory = _gate_dir(tmp_path, **kw)
    patches = case.get("patches", [])
    for p in patches:
        p.start()
    try:
        verdict = loop._run_plugin_gate(
            directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    finally:
        for p in patches:
            p.stop()
    assert verdict.passed is False
    assert verdict.code == expected
    assert codes.notice_for_plugin_gate(verdict.code).reason_code == expected


def test_plugin_gate_snapshot_and_loader_codes(improve_env, tmp_path):
    from tests.loops.test_improve_candidate_ledger import _gate_dir
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    (directory / "nested").mkdir()
    verdict = loop._run_plugin_gate(
        directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    assert verdict.code == "snapshot_rejected"
    other = tmp_path / "other"
    other.mkdir()
    bad = _gate_dir(other)
    (bad / "config.yaml").write_text("not: a plugin config\n", encoding="utf-8")
    verdict = loop._run_plugin_gate(
        bad, name="cand", source_snapshot_dir=other / "snap")
    assert verdict.code == "loader_rejected"


def test_passing_plugin_gate_verdict_has_no_code(improve_env, tmp_path):
    from tests.loops.test_improve_candidate_ledger import _gate_dir
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    with _gate_patch("run_gate_pytest",
                     lambda d, *, settings: GateResult(
                         True, 0, "99 passed in 0.1s", 0.0)):
        verdict = loop._run_plugin_gate(
            directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    assert verdict.passed is True and verdict.code is None


# --- runner の終端 status ----------------------------------------------------

BUDGET = f"tool_budget_abort:{TOKEN} 7.7777 12345"


@pytest.mark.parametrize("status,reason,expected", [
    ("timeout", None, "timeout"),
    ("timeout", BUDGET, "timeout"),
    ("timeout", "other", "timeout"),
    ("max_turns", None, "max_turns"),
    ("max_turns", BUDGET, "max_turns"),
    ("max_turns", "other", "max_turns"),
    ("failed", BUDGET, "tool_budget_abort"),
    ("failed", None, "runner_failed"),
    ("failed", "other", "runner_failed"),
    ("failed", "xtool_budget_abort:", "runner_failed"),
])
def test_failed_mission_status_priority(improve_env, status, reason, expected):
    app, root = improve_env
    conn = app.conn_core
    kwargs = {"tool_calls": 9}
    if reason is not None:
        kwargs["reason"] = reason
    loop, ctx, _ = _run(app, root, None, status=status, result_kwargs=kwargs)
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("failed", expected, 9)
    text = _all_text(row)
    assert "SECRET9" not in text and "7.7777" not in text and "12345" not in text


def test_output_invalid(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, {"bogus": 1}, result_kwargs={"tool_calls": 2})
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("failed", "output_invalid", 2)


def test_strategy_gate_sandbox_unavailable(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def unavailable(self, *a, **k):
        raise SandboxError("x", code="sandbox_unavailable",
                           sandbox_reason="host_preflight")

    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), extra=[patch.object(
            ImproveLoop, "_run_strategy_gate", unavailable)])
    assert _pair(conn, ctx.mission_id) == ("failed", "sandbox_unavailable")
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "failed"


# --- 補償 -------------------------------------------------------------------

def test_approval_creation_failure_is_approval_tx_failed(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise RuntimeError(f"injected {TOKEN}")

    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        result_kwargs={"tool_calls": 11},
        extra=[patch("agentic_fx.loops.improve_loop.approvals_store.create",
                     boom)])
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("failed", "approval_tx_failed", 11)
    assert "SECRET9" not in _all_text(row)


def _crash_commit_then_compensate(app, root, *, tool_calls_arg):
    conn = app.conn_core
    loop = _loop(app, root)
    result = MissionResult(status="completed", output=_plugin_artifact("cand"),
                           transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        mission_result = worker.run(mission)

        def boom(*a, **k):
            raise OSError(f"disk full {TOKEN}")

        loop._inspect_output = boom
        with pytest.raises(OSError):
            loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)
        kwargs = {} if tool_calls_arg is None else {"tool_calls": tool_calls_arg}
        loop.compensate_commit_failure(
            ctx=ctx, now=NOW, exc=OSError(f"disk full {TOKEN}"), **kwargs)
    return loop, ctx, conn


@pytest.mark.parametrize("passed,expected", [(None, None), (12, 12), (0, 0)])
def test_commit_crash_compensation(improve_env, passed, expected):
    app, root = improve_env
    loop, ctx, conn = _crash_commit_then_compensate(
        app, root, tool_calls_arg=passed)
    row = _snap(conn, ctx.mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("failed", "commit_crashed", expected)
    text = _all_text(row)
    assert "OSError" not in text and "disk full" not in text
    assert "SECRET9" not in text


def test_compensation_rerun_on_a_terminal_mission_adds_nothing(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output("nothing found"))
    before = dict(_snap(conn, ctx.mission_id))
    loop.compensate_commit_failure(ctx=ctx, now=NOW, exc=RuntimeError("late"),
                                   tool_calls=99)
    loop.compensate_launch_failure(ctx=ctx, now=NOW, tool_calls=99)
    rows = conn.execute("SELECT * FROM improve_run_notices").fetchall()
    assert [dict(r) for r in rows if r["mission_id"] == ctx.mission_id] == [before]


class _RaisingRunner:
    def __init__(self, **kw):
        pass

    def run(self, mission):
        raise OSError("ENOSPC injected")


def test_manual_launch_crash(improve_env):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", _RaisingRunner):
        with pytest.raises(OSError):
            sup.submit_manual()
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    row = _snap(conn, mission_id)
    assert (row["trigger"], row["result_code"], row["reason_code"],
            row["tool_calls"]) == ("manual", "failed", "launch_crashed", None)


def test_scheduled_launch_crash_records_the_scheduled_trigger(improve_env):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))
    backlog_store.add(conn, "a pending idea", "user", NOW)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=NOW, expected=1, commit=True)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", _RaisingRunner):
        with pytest.raises(OSError):
            sup._launch_slot("2026-W34", 0)
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    row = _snap(conn, mission_id)
    assert (row["trigger"], row["result_code"], row["reason_code"]) \
        == ("scheduled", "failed", "launch_crashed")
    assert conn.execute("SELECT trigger FROM improvement_runs").fetchone()[0] \
        == "scheduled"


def test_compensate_launch_failure_passes_tool_calls(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    result = MissionResult(status="completed", output=None, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
    loop.compensate_launch_failure(ctx=ctx, now=NOW, tool_calls=21)
    row = _snap(conn, ctx.mission_id)
    assert (row["reason_code"], row["tool_calls"]) == ("launch_crashed", 21)


# --- prepare() の Tx-0 -------------------------------------------------------

def _raising_runner_factory(exc):
    def build(**kw):
        raise exc
    return build


def test_prepare_trigger_manual_and_scheduled(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    result = MissionResult(status="completed", output=None, transcript=[])
    fake = lambda **kw: FakeImproveWorkerRunner(result=result, **kw)  # noqa: E731
    backlog_store.add(conn, "a pending idea", "user", NOW)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=NOW, expected=1, commit=True)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", fake):
        _, manual_ctx, _ = loop.prepare(slot_key=None, now=NOW)
        _, sched_ctx, _ = loop.prepare(slot_key=("2026-W34", 0), now=NOW)
    triggers = {r["id"]: r["trigger"] for r in conn.execute(
        "SELECT id, trigger FROM improvement_runs")}
    assert triggers[manual_ctx.run_id] == "manual"
    assert triggers[sched_ctx.run_id] == "scheduled"
    for ctx in (manual_ctx, sched_ctx):
        loop.compensate_launch_failure(ctx=ctx, now=NOW)
    assert _snap(conn, manual_ctx.mission_id)["trigger"] == "manual"
    assert _snap(conn, sched_ctx.mission_id)["trigger"] == "scheduled"


@pytest.mark.parametrize("slot", [None, ("2026-W34", 0)])
def test_prepare_failure_after_tx0_is_wrapped_and_compensated(improve_env, slot):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    if slot is not None:
        backlog_store.add(conn, "a pending idea", "user", NOW)
        improve_waves.create_wave_and_slots(
            conn, period_key=slot[0], now=NOW, expected=1, commit=True)
    boom = RuntimeError(f"injected {TOKEN}")
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               _raising_runner_factory(boom)):
        with pytest.raises(ImprovePrepareFailed) as info:
            loop.prepare(slot_key=slot, now=NOW)
    assert info.value.__cause__ is boom
    assert info.value.slot_key == slot
    mission_id = info.value.mission_id
    row = _snap(conn, mission_id)
    assert (row["result_code"], row["reason_code"], row["tool_calls"]) \
        == ("failed", "prepare_failed", None)
    assert row["trigger"] == ("manual" if slot is None else "scheduled")
    assert "SECRET9" not in _all_text(row)
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mission_id,)).fetchone()[0] == "failed"
    assert conn.execute("SELECT finished_at FROM improvement_runs WHERE "
                        "mission_id=?", (mission_id,)).fetchone()[0] is not None


def test_prepare_failure_is_an_exception_type_with_attributes():
    assert issubclass(ImprovePrepareFailed, Exception)
    exc = ImprovePrepareFailed(7, ("2026-W34", 0))
    assert (exc.mission_id, exc.slot_key) == (7, ("2026-W34", 0))


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_pass_through_prepare_after_compensation(
        improve_env, exc_type):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               _raising_runner_factory(exc_type())):
        with pytest.raises(exc_type) as info:
            loop.prepare(slot_key=None, now=NOW)
    assert not isinstance(info.value, ImprovePrepareFailed)
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    assert _pair(conn, mission_id) == ("failed", "prepare_failed")
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mission_id,)).fetchone()[0] == "failed"


def test_prepare_failure_with_failing_compensation_leaves_the_mission_running(
        improve_env):
    from agentic_fx.store import missions as missions_store
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)

    def failing(*a, **k):
        raise sqlite3.OperationalError("compensation broke")

    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               _raising_runner_factory(RuntimeError("x"))), \
            patch.object(missions_store, "finish_improve_mission", failing):
        with pytest.raises(ImprovePrepareFailed):
            loop.prepare(slot_key=None, now=NOW)
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mission_id,)).fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM improve_run_notices"
                        ).fetchone()[0] == 0


def test_failure_before_tx0_leaves_no_mission_and_no_wrapper(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=NOW, expected=1, commit=True)
    with patch.object(improve_waves, "claim_slot", lambda *a, **k: False):
        with pytest.raises(RuntimeError, match="slot claim failed"):
            loop.prepare(slot_key=("2026-W34", 0), now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM improve_run_notices"
                        ).fetchone()[0] == 0

"""[indicator-consumption-wiring] T6b: `wiring_envs` の各ビルダの smoke test。

opus r1 I3: 元案は `switch_env` 1 本しか検証しておらず、他の 21 ビルダは
「壊れたまま緑」で T4/T5 へ渡る構造だった。各ビルダについて**返り値を使って
1 手進める**ところまで検査する。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentic_fx.activity import Category
from tests.fixtures import indicator_wiring as fx
from tests.fixtures import wiring_envs as env


def test_switch_env_produces_isolated_roots(tmp_path):
    conn, plugins_root = env.switch_env(tmp_path / "a")
    assert plugins_root.is_dir() and plugins_root.name == "plugins"
    assert (plugins_root / "_human").is_dir()
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert str(plugins_root).startswith(str(tmp_path))


def test_install_gate_double_makes_a_strategy_submit_fast_and_green(
        tmp_path, monkeypatch):
    """codex plan r1 C1: gate double が `submit_candidate` を実 backtest
    無しで通す。

    **ここで `resolved` は見ない** — T6b は T3 と並列、T4a より前にマージ
    される task なので、この時点では `evaluate_strategy_adoption_gate` に
    `resolved` が渡っていない可能性がある (T3 未マージ) し、実解決が
    行われるのは T4a 以降。「resolver は double より前に 1 回だけ走る」は
    **T4a Step 4-1a の `test_run_kind_gate_resolves_once_and_carries_the_
    same_object` が pin する** — ここでは double 自体が機能することだけを
    見る ([[measure-capability-not-startup]]: 「返り値で 1 手進める」=
    approval 行が実際に作られるところまで)。"""
    from agentic_fx.plugin import switch as plugin_switch
    conn, plugins_root = env.switch_env(tmp_path / "gd")
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": hashes["rsi"]})
    seen = env.install_gate_double(monkeypatch)
    approval_id = plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=env.SETTINGS_FIXTURE, now=fx.NOW)
    assert approval_id > 0
    assert len(seen) == 1                      # gate は 1 回だけ呼ばれた
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "pending"


def test_reconcile_env_activity_is_writable(tmp_path):
    conn, plugins_root, activity = env.reconcile_env(tmp_path / "b")
    activity.write(Category.APPROVAL, "probe", "hello world")
    assert "probe" in env.activity_text(activity)


def test_activity_text_returns_written_lines(tmp_path):
    """opus r1 C3: `ActivityLog.read_text()` は存在しない。行の**形**
    (tab 区切り 5 列、3 列目 = event、4 列目 = summary) をここで 1 度だけ
    固定し、R2 / F5 / C1 の逐語 pin はこの形を前提に書く。"""
    _conn, _root, activity = env.reconcile_env(tmp_path / "c")
    activity.write(Category.IMPROVE, "backtest_cpu",
                   "mission=1 plugin=rsi_pullback cpu_sec=1.5")
    line = env.activity_text(activity).splitlines()[-1]
    cols = line.split("\t")
    assert len(cols) == 5
    assert cols[1] == "IMPROVE"
    assert cols[2] == "backtest_cpu"
    assert cols[3] == "mission=1 plugin=rsi_pullback cpu_sec=1.5"
    assert cols[4] == "-"


def test_improve_env_survives_a_closed_handler_conn(tmp_path):
    """opus r1 I6: 親の `run_backtest_handler` は自分で開いた conn を
    `finally` で閉じる。factory が毎回新規接続を返さないと、1 回回した
    時点でテスト側の conn まで閉じる。"""
    loop, conn, root = env.improve_env(tmp_path / "d")
    handler_conn = loop._db_write_conn_factory()
    handler_conn.close()
    # テスト側の conn は生きている
    assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_improve_env_with_activity_exposes_the_loop_activity_object(tmp_path):
    """T6b 完了条件: 22 ビルダそれぞれに最小 1 本の smoke test が要る。
    `improve_env_with_activity` は Step 6b-1a のテスト本文に直接の smoke
    test が無かった (`improve_env` / `reconcile_env` で代替できない —
    このビルダ固有の契約は「`loop._activity` と同一オブジェクトを返す」)。
    """
    loop, _conn, _root, activity = env.improve_env_with_activity(tmp_path / "d2")
    assert activity is loop._activity
    activity.write(Category.IMPROVE, "probe", "hello")
    assert "probe" in env.activity_text(activity)


def test_loop_env_is_a_thin_alias_returning_the_plugins_subdir(tmp_path):
    """T6b 完了条件: `loop_env` にも Step 6b-1a のテスト本文に直接の
    smoke test が無かった。`improve_env` との違い (3 要素目が `plugins_root`
    であること) をここで固定する。"""
    loop, conn, plugins_root = env.loop_env(tmp_path / "d3")
    assert plugins_root == (tmp_path / "d3" / "plugins")
    assert plugins_root.is_dir()
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert loop._root == tmp_path / "d3"


def test_prepare_ctx_builds_a_run_context(tmp_path):
    """opus r1 C1: `prepare` は 3-tuple を返し `conn` 引数を持たない。
    tmp 環境で `WorkerRunner` 構築まで通ることをここで確かめる
    (通らなければ `synthetic_ctx` へ切り替え、指揮者へ申告する)。"""
    loop, _conn, root = env.improve_env(tmp_path / "e")
    ctx = env.prepare_ctx(loop, now=fx.NOW)
    assert ctx.staging_dir.is_dir()
    assert ctx.source_snapshot_dir.is_dir()
    assert ctx.mission_id > 0 and ctx.run_id > 0


def test_synthetic_ctx_is_usable_without_prepare(tmp_path):
    loop, conn, root = env.improve_env(tmp_path / "f")
    ctx = env.synthetic_ctx(loop, conn, root)
    assert ctx.staging_dir.is_dir() and ctx.rpc_handlers == {}


def test_shell_env_dispatches(tmp_path):
    """opus r1 C4: `commands.Shell` は存在しない。`Commands.dispatch` が
    動くところまで進める。"""
    cmds, conn, plugins_root = env.shell_env(tmp_path / "g")
    assert "learning" in cmds.dispatch("status")
    assert cmds.plugins_root == plugins_root
    assert cmds.settings is env.SETTINGS_FIXTURE


def test_rpc_tools_expose_run_backtest(tmp_path):
    from agentic_fx.tools.mission_counters import MissionToolCounters
    calls = []
    tools = env.rpc_tools(tmp_path / "h", counters=MissionToolCounters(),
                          run_backtest_handler=lambda a: calls.append(a)
                          or {"started": True})
    assert "run_backtest" in tools
    tools["run_backtest"](name="cand", pair="USDJPY")
    assert calls and calls[0]["name"] == "cand"


def test_rpc_tooldefs_can_be_registered(tmp_path):
    """opus r1 I5: F4 の `errors` / refusal streak は `ToolRegistry` の
    `on_result` 経由でしか増えない。tooldef のリストがそのまま
    `ToolRegistry` に渡せることをここで据える。"""
    from agentic_fx.tools.mission_counters import MissionToolCounters
    from agentic_fx.tools.registry import ToolRegistry
    counters = MissionToolCounters()
    defs = env.rpc_tooldefs(tmp_path / "h2", counters=counters,
                            run_backtest_handler=lambda a: {
                                "started": False, "error": "indicator_unresolved"})
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)
    registry.execute("run_backtest", {"name": "cand", "pair": "USDJPY"},
                     allowed=registry.names())
    assert counters.errors == 1


def test_deploy_strategy_is_discoverable_and_hashes_match(tmp_path):
    from agentic_fx.plugin.loader import content_hash, discover_one_with_reason
    conn, plugins_root = env.switch_env(tmp_path / "i")
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    chash = env.deploy_strategy(conn, plugins_root, "rsi_pullback",
                                pins={"rsi": hashes["rsi"]})
    meta, reason = discover_one_with_reason(plugins_root / "rsi_pullback",
                                            "rsi_pullback")
    assert reason is None and meta.content_hash == chash
    assert chash == content_hash((plugins_root / "rsi_pullback").resolve())


def test_bump_indicator_version_changes_hash_and_symlink(tmp_path):
    conn, plugins_root = env.switch_env(tmp_path / "j")
    fx.write_indicator(plugins_root, "rsi")
    before = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)["rsi"]
    after = env.bump_indicator_version(conn, plugins_root, "rsi",
                                       now=fx.NOW + timedelta(hours=1))
    assert after != before
    assert (plugins_root / "rsi").is_symlink()


def test_submit_and_approve_indicator_v2_round_trip(tmp_path):
    # `agentic_fx.store.approvals` に `get(conn, id)` は**存在しない**
    # (公開 API は create / apply_decision / pending / expire_due /
    # list_due_for_expiry / set_reason。着手時に
    # `rg -n '^def ' src/agentic_fx/store/approvals.py` で再取得)。
    # 既存テスト (`tests/test_commands.py`) と同じ SQL で status を読む。
    def _status(conn, aid):
        return conn.execute(
            "SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()[0]

    conn, plugins_root = env.switch_env(tmp_path / "k")
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    approval_id, chash = env.submit_indicator_v2(conn, plugins_root, "rsi")
    assert _status(conn, approval_id) == "pending"
    env.approve_indicator_v2(conn, plugins_root, "rsi",
                             approval_id=approval_id)
    assert _status(conn, approval_id) == "approved"


@pytest.mark.slow
def test_stage_switched_journal_reconciles_to_decided_when_pin_intact(tmp_path):
    """opus r1 I8: このヘルパは R2 の成否を丸ごと決める。**pin が破れて
    いないとき reconcile が `decided` まで進む**ことをここで据える
    (T4b Step 4-7 の R2 は「破れているとき reverted」を見る裏返し)。"""
    from agentic_fx.plugin import switch as plugin_switch
    conn, plugins_root = env.switch_env(tmp_path / "l")
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    old_target, new_target, approval_id, op_id = env.stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    # 現物は `reconcile_switch_journals(conn, *, plugins_root, now, settings,
    # activity=None, force_revert_op_id=None)` (着手時に
    # `rg -n 'def reconcile_switch_journals' -A 5 src/agentic_fx/plugin/switch.py`
    # で再取得)。T4b Step 4-7 で引数が増えたら本テストも更新すること。
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, settings=env.SETTINGS_FIXTURE,
        now=fx.NOW + timedelta(minutes=1))
    # テーブル名は **`plugin_switch_journal`** (`store/db.py` の DDL)
    row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id = ?",
        (op_id,)).fetchone()
    assert row[0] == "decided", (
        "pin が破れていないのに decided へ進まない — payload の content_hash か "
        "advance_switch_journal(commit=True) を疑う (opus r1 I8)")
    assert (plugins_root / "rsi_pullback").readlink().name \
        == new_target.rsplit("/", 1)[-1]


def test_copy_example_does_not_touch_the_repo(tmp_path):
    dest = env.copy_example(tmp_path / "m", "rsi_pullback")
    assert (dest / "plugin.py").exists()
    assert str(dest).startswith(str(tmp_path))


def test_rename_dependency_rewrites_config(tmp_path):
    import yaml
    d = fx.write_rsi_pullback(tmp_path / "n", pins=None)
    env.rename_dependency(d, "rsi", "rsi_other")
    config = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8"))
    assert config["indicators"]["rsi"]["plugin"] == "rsi_other"


def test_write_dependency_free_strategy_discovers_with_no_indicators(tmp_path):
    from agentic_fx.plugin.loader import discover_one_with_reason
    d = env.write_dependency_free_strategy(tmp_path / "o", "plain")
    meta, reason = discover_one_with_reason(d, "plain")
    assert reason is None and meta.indicators == ()


def test_completed_result_and_mission_for_are_accepted_shapes(tmp_path):
    loop, conn, root = env.improve_env(tmp_path / "p")
    ctx = env.synthetic_ctx(loop, conn, root)
    result = env.completed_result({"summary": "x"})
    assert result.status == "completed"
    assert env.mission_for(ctx).max_turns == 1

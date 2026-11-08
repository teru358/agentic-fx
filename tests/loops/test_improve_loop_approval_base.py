"""改善 mission の提出が、mission 開始時に写した版を土台として保存する。"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.plugin import base_identity, loader, version_store
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from agentic_fx.runners.base import Mission, MissionResult

NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)
NAME = "myind"


def _write_files(dest: Path, value: float) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "plugin.py").write_text(
        f"def compute(df, params):\n    return {{'v': {value}}}\n")
    (dest / "config.yaml").write_text(
        "kind: indicator\npairs: []\ntimeframe: '1h'\noutputs: ['v']\n")
    (dest / "test_plugin.py").write_text(
        "def test_x():\n    pass\ndef test_y():\n    pass\ndef test_z():\n    pass\n")


def _install_version(plugins: Path, value: float) -> str:
    scratch = plugins.parent / f"scratch-{value}"
    _write_files(scratch, value)
    files = [(scratch / n).read_bytes()
             for n in ("plugin.py", "config.yaml", "test_plugin.py")]
    artifact = loader.artifact_hash_bytes(*files)
    version_store.create_version_dir(
        plugins, NAME, artifact, plugin_py=files[0], config_yaml=files[1],
        test_plugin=files[2], op_identity="t")
    return artifact


def _discover(plugins: Path):
    return tuple(loader.discover(plugins))


def _commit(loop, conn, fixture, tmp_path, monkeypatch, metas):
    mission_id, run_id, backlog_id = fixture
    staging_dir = tmp_path / "staging"
    _write_files(staging_dir / NAME, 99.0)
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={
            "analyze_corr": 60.0, "run_backtest": 600.0}),
        rpc_handlers={},
        inventory=InventoryBuildResult(
            inventory=ApprovedInventory(root=tmp_path / "plugins", metas=()),
            phase1_metas=metas, resolved={}, rejected_strategies=()))
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [], "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": NAME, "kind": "indicator",
                     "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    loop.commit(mission=mission, ctx=ctx, result=result, now=NOW)
    row = conn.execute(
        "SELECT payload_json FROM approval_requests ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["payload_json"]) if row is not None else None


def test_base_is_the_version_copied_at_mission_start_not_the_live_one(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """受入3: mission の途中で live が別の版に切り替わっても、土台は開始時の版。"""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    started_with = _install_version(plugins, 1.0)
    (plugins / NAME).symlink_to(f".versions/{NAME}/{started_with}")
    snapshot = _discover(plugins)
    assert [m.name for m in snapshot] == [NAME]
    switched_to = _install_version(plugins, 2.0)
    (plugins / NAME).unlink()
    (plugins / NAME).symlink_to(f".versions/{NAME}/{switched_to}")

    payload = _commit(loop_full, conn, mission_and_run_fixture, tmp_path,
                      monkeypatch, snapshot)

    assert payload["base"] == {"kind": "version", "artifact_hash": started_with}


def test_base_is_none_when_the_name_was_not_copied(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    started_with = _install_version(plugins, 1.0)
    (plugins / NAME).symlink_to(f".versions/{NAME}/{started_with}")

    payload = _commit(loop_full, conn, mission_and_run_fixture, tmp_path,
                      monkeypatch, ())

    assert payload["base"] == {"kind": "none"}


def test_base_is_plain_when_the_copied_version_came_from_a_plain_directory(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _write_files(plugins / NAME, 1.0)
    snapshot = _discover(plugins)
    assert len(snapshot) == 1

    payload = _commit(loop_full, conn, mission_and_run_fixture, tmp_path,
                      monkeypatch, snapshot)

    assert payload["base"] == {"kind": "plain",
                               "content_hash": snapshot[0].content_hash}


def test_unresolvable_base_fails_the_submission_without_an_approval(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """土台を導けない形は提出の時点で失敗させ、承認依頼を作らない。"""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    started_with = _install_version(plugins, 1.0)
    (plugins / NAME).symlink_to(f".versions/{NAME}/{started_with}")
    (meta,) = _discover(plugins)
    odd = replace(meta, path=tmp_path / "somewhere" / "else")

    payload = _commit(loop_full, conn, mission_and_run_fixture, tmp_path,
                      monkeypatch, (odd,))

    assert payload is None
    last = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (mission_and_run_fixture[2],)).fetchone()[0]
    assert last == "gate_failed:unverified"
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_a_strategy_whose_dependency_pin_broke_still_gives_its_base_from_the_snapshot(
        conn, tmp_path):
    """受入10: 依存 pin が破れた strategy は配備の対象外でも snapshot に残り、
    pin だけを変える再ロックの土台は mission 開始時の版から取れる。"""
    from agentic_fx.store import approvals as approvals_store
    from agentic_fx.tools.plugin_loader import approved_plugins
    from tests.backtest.factories import SETTINGS

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    dep_v1 = _install_version(plugins, 1.0)
    scratch_dep_v1 = loader.content_hash(plugins.parent / "scratch-1.0")

    def install_strategy(pin: str) -> tuple[str, str]:
        scratch = plugins.parent / "scratch-strategy"
        scratch.mkdir(exist_ok=True)
        (scratch / "plugin.py").write_text(
            "def evaluate(df, indicators, signals, params):\n"
            "    return {'action': 'hold', 'rationale': 'x'}\n")
        (scratch / "config.yaml").write_text(
            "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
            f"max_bars: 200\nindicators:\n  e: {{plugin: {NAME}, pin: '{pin}'}}\n")
        (scratch / "test_plugin.py").write_text("def test_x():\n    pass\n")
        files = [(scratch / n).read_bytes()
                 for n in ("plugin.py", "config.yaml", "test_plugin.py")]
        artifact = loader.artifact_hash_bytes(*files)
        version_store.create_version_dir(
            plugins, "strat", artifact, plugin_py=files[0], config_yaml=files[1],
            test_plugin=files[2], op_identity="t")
        return artifact, loader.content_hash(scratch)

    strat_artifact, strat_content = install_strategy(scratch_dep_v1)
    (plugins / NAME).symlink_to(f".versions/{NAME}/{dep_v1}")
    (plugins / "strat").symlink_to(f".versions/strat/{strat_artifact}")
    dep_v2 = _install_version(plugins, 2.0)
    (plugins / NAME).unlink()
    (plugins / NAME).symlink_to(f".versions/{NAME}/{dep_v2}")
    for name, content in ((NAME, loader.content_hash(plugins / NAME)),
                          ("strat", strat_content)):
        approval_id = approvals_store.create(
            conn, "plugin", {"name": name, "content_hash": content}, NOW)
        approvals_store.apply_decision(conn, approval_id, "approved",
                                       decided_by="h", now=NOW)

    result = approved_plugins(conn, plugins, settings=SETTINGS)

    assert [r.name for r in result.rejected_strategies] == ["strat"]
    assert "strat" not in [m.name for m in result.inventory.metas]
    assert base_identity.base_of_snapshot(
        result.phase1_metas, "strat", plugins) == {
            "kind": "version", "artifact_hash": strat_artifact}

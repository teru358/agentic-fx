from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.plugin import deploy_disable
from agentic_fx.plugin import loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store
from agentic_fx.store import missions as missions_store
from tests.plugin.conftest import SETTINGS


NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)


def _version(root, name="sma"):
    version = root / ".versions" / name / ("a" * 64)
    version.mkdir(parents=True)
    (version / "plugin.py").write_text("def compute(df, params):\n    return {'value': df}\n")
    (version / "config.yaml").write_text("kind: indicator\noutputs: [value]\n")
    (version / "test_plugin.py").write_text("def test_x():\n    pass\n")
    return version


def _version_with_kind(root: Path, name: str, artifact: str, *, kind: str,
                       indicator: str | None = None) -> Path:
    version = root / ".versions" / name / artifact
    version.mkdir(parents=True)
    if kind == "indicator":
        source = "def compute(df, params):\n    return {'value': 1.0}\n"
        config = "kind: indicator\noutputs: [value]\n"
    elif kind == "signal":
        source = "def detect(df, params):\n    return []\n"
        config = "kind: signal\ntimeframe: 1h\npairs: [USDJPY]\n"
    else:
        source = "def evaluate(df, indicators, signals, params):\n    return {'action': 'hold'}\n"
        config = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
                  "exit_mode: levels\n"
                  + (f"indicators:\n  fast: {{plugin: {indicator}}}\n" if indicator else ""))
    (version / "plugin.py").write_text(source)
    (version / "config.yaml").write_text(config)
    (version / "test_plugin.py").write_text("def test_plugin():\n    pass\n")
    digest = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (version / "plugin.py", version / "config.yaml", version / "test_plugin.py")))
    final = version.with_name(digest)
    version.rename(final)
    return final


def _approve(conn, name: str, version: Path, *, now=NOW) -> tuple[str, str, int]:
    content = loader.content_hash(version)
    artifact = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (version / "plugin.py", version / "config.yaml", version / "test_plugin.py")))
    approval_id = approvals_store.create(
        conn, "plugin", {"name": name, "content_hash": content,
                         "artifact_hash": artifact}, now)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="human", now=now)
    return content, artifact, approval_id


def _link_live(root: Path, name: str, version: Path) -> Path:
    live = root / name
    live.symlink_to(f".versions/{name}/{version.name}")
    return live


def _signal(conn, name: str, content: str, *, claimed: bool = False) -> int:
    signal_id = signals_store.add(
        conn, plugin=name, content_hash=content, pair="USDJPY", timeframe="1h",
        bar_ts=(NOW + timedelta(minutes=1) if claimed else NOW).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)
    if claimed:
        conn.execute("UPDATE signals SET status='claimed' WHERE id=?", (signal_id,))
        conn.commit()
    return signal_id


class _Activity:
    def __init__(self):
        self.events = []

    def write(self, category, event, summary):
        self.events.append((category, event, summary))


def test_disable_unlinks_live_symlink_and_records_done_op(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version(root)
    live = root / "sma"
    live.symlink_to(".versions/sma/" + version.name)
    content = loader.content_hash(version)
    artifact = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (version / "plugin.py", version / "config.yaml", version / "test_plugin.py")))
    aid = approvals_store.create(conn, "plugin", {"name": "sma", "content_hash": content,
                                                     "artifact_hash": artifact}, NOW)
    approvals_store.apply_decision(conn, aid, "approved", decided_by="human", now=NOW)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents", lambda *a, **k: deploy_disable.DependentsReport())

    outcome = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                            now=NOW, settings=object())

    assert outcome.filesystem_switched is True
    assert not live.exists()
    assert version.is_dir()
    row = deploy_store.list_by_name(conn, "sma")[0]
    assert row["op_kind"] == "disable_symlink"
    assert row["phase"] == "done"
    assert row["notify_attempted"] == 1


def test_strategy_identities_select_each_named_latest_approval(tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    for name, content in (("first", "first-content"), ("second", "second-content")):
        approval_id = approvals_store.create(
            conn, "plugin", {"name": name, "content_hash": content}, NOW)
        approvals_store.apply_decision(conn, approval_id, "approved", decided_by="human", now=NOW)

    assert list(deploy_disable._strategy_identities(conn, {"first", "second"})) == [
        ("first", "first-content"), ("second", "second-content")]


def test_disable_returns_retry_outcome_when_unlink_fails(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version(root)
    live = root / "sma"
    live.symlink_to(".versions/sma/" + version.name)
    content = loader.content_hash(version)
    artifact = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (version / "plugin.py", version / "config.yaml", version / "test_plugin.py")))
    approval_id = approvals_store.create(conn, "plugin", {"name": "sma", "content_hash": content,
                                                            "artifact_hash": artifact}, NOW)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="human", now=NOW)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents", lambda *a, **k: deploy_disable.DependentsReport())
    monkeypatch.setattr(deploy_disable.os, "unlink", lambda _path: (_ for _ in ()).throw(OSError("blocked")))

    outcome = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                            now=NOW, settings=object())

    assert outcome.outcome == "needs_retry"
    assert deploy_store.get_open_by_name(conn, "sma")["fail_reason"] == "syscall_failed"


def test_disable_dry_run_does_not_reopen_syscall_failed_op(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version(root)
    live = root / "sma"
    target = ".versions/sma/" + version.name
    live.symlink_to(target)
    content = loader.content_hash(version)
    artifact = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (version / "plugin.py", version / "config.yaml", version / "test_plugin.py")))
    op_id = deploy_store.insert_intent(
        conn, op_kind="disable_symlink", name="sma", from_kind="symlink", to_kind="absent",
        from_target=target, to_target=None, retired_path=None, subject_content_hash=content,
        subject_artifact_hash=artifact, target_content_hash=None, target_artifact_hash=None,
        approval_id=None, revoke=False, revoke_row_id=None, force=False, actor="human_cli", now=NOW)
    deploy_store.set_phase(conn, op_id, "needs_retry", now=NOW, fail_reason="syscall_failed")
    monkeypatch.setattr(deploy_disable, "enumerate_dependents", lambda *a, **k: deploy_disable.DependentsReport())

    outcome = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                            now=NOW, settings=object(), dry_run=True)

    assert outcome.outcome == "dry_run"
    assert deploy_store.get_open_by_name(conn, "sma")["phase"] == "needs_retry"


def test_disable_preserves_version_and_approval_and_emits_done_activity(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    content, _, approval_id = _approve(conn, "sma", version)
    live = _link_live(root, "sma", version)
    activity = _Activity()
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *args, **kwargs: deploy_disable.DependentsReport())

    outcome = deploy_disable.disable_plugin(
        conn, root, "sma", revoke=False, force=False, now=NOW,
        settings=SETTINGS, activity=activity)

    assert outcome.outcome == "disabled"
    assert not os.path.lexists(live)
    assert version.is_dir()
    approvals = conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchall()
    assert len(approvals) == 1
    assert approvals[0]["status"] == "approved"
    assert json.loads(approvals[0]["payload_json"])["content_hash"] == content
    row = deploy_store.list_by_name(conn, "sma")[0]
    assert row["phase"] == "done" and row["notify_attempted"] == 1
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_done"]


def test_second_disable_is_idempotent_and_revoke_requires_last_disabled_version(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    content, _, _ = _approve(conn, "sma", version)
    _link_live(root, "sma", version)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *args, **kwargs: deploy_disable.DependentsReport())
    deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                  now=NOW, settings=SETTINGS)
    before = conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0]

    again = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                          now=NOW, settings=SETTINGS)
    revoke = deploy_disable.disable_plugin(conn, root, "sma", revoke=True, force=False,
                                           now=NOW, settings=SETTINGS)

    assert again.outcome == "already_disabled"
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == before
    assert revoke.outcome == "revoke_requires_version"
    assert revoke.version_candidate == content


def test_disable_abandons_pending_and_hides_both_linked_signal_statuses(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version_with_kind(root, "sig", "a" * 64, kind="signal")
    content, _, _ = _approve(conn, "sig", version)
    _link_live(root, "sig", version)
    pending, claimed = _signal(conn, "sig", content), _signal(conn, "sig", content, claimed=True)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *args, **kwargs: deploy_disable.DependentsReport())

    deploy_disable.disable_plugin(conn, root, "sig", revoke=False, force=False,
                                  now=NOW, settings=SETTINGS)

    statuses = dict(conn.execute("SELECT id,status FROM signals").fetchall())
    assert statuses == {pending: "abandoned", claimed: "claimed"}
    assert signals_store.recent(conn, "USDJPY", since=NOW, exclude_deploy_linked=True) == []
    mission = missions_store.start(conn, "trade", "local", "model", NOW)
    assert signals_store.claim_oldest(conn, mission_id=mission, now=NOW, freshness_bars=1) is None
    assert signals_store.pending_exists(conn, now=NOW, freshness_bars=1) is False
    op_id = deploy_store.list_by_name(conn, "sig")[0]["op_id"]
    assert {tuple(row) for row in conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals WHERE op_id=?", (op_id,))} == {
        (pending, "pending"), (claimed, "claimed")}


def test_disable_blocks_dependents_then_force_cascades_in_single_transaction(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    indicator = _version_with_kind(root, "a", "a" * 64, kind="indicator")
    strategy = _version_with_kind(root, "s", "b" * 64, kind="strategy", indicator="a")
    a_content, _, _ = _approve(conn, "a", indicator)
    s_content, _, _ = _approve(conn, "s", strategy)
    a_live, s_live = _link_live(root, "a", indicator), _link_live(root, "s", strategy)
    a_pending = _signal(conn, "a", a_content)
    s_pending = _signal(conn, "s", s_content)
    s_claimed = _signal(conn, "s", s_content, claimed=True)
    other_pending = _signal(conn, "other", "other-content")
    other_claimed = _signal(conn, "other", "other-content", claimed=True)

    blocked = deploy_disable.disable_plugin(conn, root, "a", revoke=False, force=False,
                                            now=NOW, settings=SETTINGS)
    assert blocked.outcome == "dependents_blocked"
    assert blocked.dependents == (("s", "fast", "not_found"),)
    assert a_live.is_symlink() and s_live.is_symlink()
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0

    real_abandon = signals_store.abandon_pending_for_identities
    monkeypatch.setattr(signals_store, "abandon_pending_for_identities",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("abort")))
    with pytest.raises(RuntimeError, match="abort"):
        deploy_disable.disable_plugin(conn, root, "a", revoke=False, force=True,
                                      now=NOW, settings=SETTINGS)
    assert a_live.is_symlink() and s_live.is_symlink()
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 0
    monkeypatch.setattr(signals_store, "abandon_pending_for_identities", real_abandon)

    forced = deploy_disable.disable_plugin(conn, root, "a", revoke=False, force=True,
                                           now=NOW, settings=SETTINGS)

    assert forced.outcome == "disabled" and forced.cascade_strategies == ("s",)
    assert not os.path.lexists(a_live) and s_live.is_symlink()
    statuses = dict(conn.execute("SELECT id,status FROM signals").fetchall())
    assert statuses == {
        a_pending: "abandoned", s_pending: "abandoned", s_claimed: "claimed",
        other_pending: "pending", other_claimed: "claimed"}
    op_id = deploy_store.list_by_name(conn, "a")[0]["op_id"]
    assert {tuple(row) for row in conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals WHERE op_id=?", (op_id,))} == {
        (a_pending, "pending"), (s_pending, "pending"), (s_claimed, "claimed")}


def test_retry_reopens_only_same_signature_and_notifies_after_success(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    _approve(conn, "sma", version)
    live = _link_live(root, "sma", version)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *args, **kwargs: deploy_disable.DependentsReport())
    real_unlink = deploy_disable.os.unlink
    calls = {"n": 0}

    def fail_once(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("blocked")
        return real_unlink(path)

    monkeypatch.setattr(deploy_disable.os, "unlink", fail_once)
    activity = _Activity()
    first = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                          now=NOW, settings=SETTINGS, activity=activity)
    row = deploy_store.get_open_by_name(conn, "sma")
    assert first.outcome == "needs_retry" and row["notify_attempted"] == 1
    with pytest.raises(deploy_disable.deploy_ops.SignatureMismatchError):
        deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=True,
                                      now=NOW, settings=SETTINGS)

    second = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                           now=NOW, settings=SETTINGS, activity=activity)
    rows = deploy_store.list_by_name(conn, "sma")
    assert second.outcome == "disabled" and not os.path.lexists(live)
    assert len(rows) == 1 and rows[0]["phase"] == "done" and rows[0]["notify_attempted"] == 1
    assert [event for _, event, _ in activity.events] == [
        "plugin_deploy_op_needs_retry", "plugin_deploy_op_done"]


def test_foreign_replacement_before_unlink_is_closed_without_touching_directory(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    _approve(conn, "sma", version)
    live = _link_live(root, "sma", version)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *args, **kwargs: deploy_disable.DependentsReport())
    activity = _Activity()
    real_readlink = deploy_disable.os.readlink
    calls = {"n": 0}

    def replace_after_identity(path):
        calls["n"] += 1
        target = real_readlink(path)
        if calls["n"] == 2:
            live.unlink()
            live.mkdir()
            (live / "keep").write_text("unchanged")
        return target

    monkeypatch.setattr(deploy_disable.os, "readlink", replace_after_identity)
    outcome = deploy_disable.disable_plugin(conn, root, "sma", revoke=False, force=False,
                                            now=NOW, settings=SETTINGS, activity=activity)

    assert outcome.outcome == "foreign"
    assert live.is_dir() and (live / "keep").read_text() == "unchanged"
    row = deploy_store.list_by_name(conn, "sma")[0]
    assert row["phase"] == "foreign" and row["notify_attempted"] == 1
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_foreign"]

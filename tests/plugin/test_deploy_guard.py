"""配備ガード: 配備操作は共有、質検査の母集団確定は排他。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.plugin import switch


def test_deploy_operations_do_not_exclude_each_other(tmp_path):
    root = tmp_path / "plugins"
    inside = threading.Event()
    release = threading.Event()

    def other():
        with switch._plugin_lock(root, "beta"):
            inside.set()
            release.wait(10)

    thread = threading.Thread(target=other)
    thread.start()
    try:
        assert inside.wait(5)
        with switch._plugin_lock(root, "alpha", deadline=_soon()):
            pass
    finally:
        release.set()
        thread.join(10)


def _soon():
    import time
    return time.monotonic() + 2


def test_a_plugin_lock_excludes_the_exclusive_guard(tmp_path):
    root = tmp_path / "plugins"
    with switch._plugin_lock(root, "alpha"):
        import time
        with pytest.raises(switch.PluginBusyError):
            with switch.deploy_guard(
                    root, exclusive=True, deadline=time.monotonic() + 0.2):
                pass


def test_the_exclusive_guard_excludes_a_plugin_lock_until_released(tmp_path):
    import time
    root = tmp_path / "plugins"
    with switch.deploy_guard(root, exclusive=True):
        with pytest.raises(switch.PluginBusyError):
            with switch._plugin_lock(
                    root, "alpha", deadline=time.monotonic() + 0.2):
                pass
    with switch._plugin_lock(root, "alpha", deadline=time.monotonic() + 2):
        pass


def test_the_guard_file_is_not_collected_as_an_orphan_lock(tmp_path):
    assert not switch.deploy_guard_path(tmp_path).name.endswith(".lock")


# --- ガードを通らない配備変更経路を残さない ---------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from agentic_fx.store import approvals as approvals_store  # noqa: E402
from agentic_fx.store import db as db_store  # noqa: E402

_NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
_PAST = _NOW - timedelta(hours=1)


def _env(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    return plugins, conn


def _expired_pending(conn, name="sma"):
    return approvals_store.create(
        conn, kind="plugin",
        payload={"name": name, "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": f"plugins/_staging/1/{name}"},
        now=_PAST, expires_at=_PAST)


def _spy_guard(monkeypatch):
    calls = []
    real = switch.deploy_guard

    def spy(plugins_root, *, exclusive, **kwargs):
        calls.append(exclusive)
        return real(plugins_root, exclusive=exclusive, **kwargs)

    monkeypatch.setattr(switch, "deploy_guard", spy)
    return calls


def _runs_only_after_release(root, call):
    """排他ガードを保持している間は `call` が終わらず、解放後に終わる。"""
    done = threading.Event()
    errors = []

    def target():
        try:
            call()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    with switch.deploy_guard(root, exclusive=True):
        thread = threading.Thread(target=target)
        thread.start()
        blocked = not done.wait(0.4)
    assert done.wait(10)
    thread.join(10)
    assert not errors, errors
    assert blocked


def test_sweep_orphans_takes_the_guard_shared(tmp_path, monkeypatch):
    plugins, conn = _env(tmp_path)
    calls = _spy_guard(monkeypatch)
    switch.sweep_orphans(conn, plugins_root=plugins, now=_NOW)
    assert calls == [False]


def test_sweep_orphans_waits_while_the_exclusive_guard_is_held(tmp_path):
    plugins, conn = _env(tmp_path)
    _runs_only_after_release(plugins, lambda: switch.sweep_orphans(
        conn, plugins_root=plugins, now=_NOW))


def test_expiring_an_approval_takes_the_guard_shared_before_the_name_lock(
        tmp_path, monkeypatch):
    plugins, conn = _env(tmp_path)
    approval_id = _expired_pending(conn)
    calls = _spy_guard(monkeypatch)
    switch.process_expired_approvals(conn, plugins_root=plugins, now=_NOW)
    assert calls == [False]
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "expired"


def test_expiring_an_approval_waits_while_the_exclusive_guard_is_held(tmp_path):
    plugins, conn = _env(tmp_path)
    approval_id = _expired_pending(conn)
    _runs_only_after_release(plugins, lambda: switch.process_expired_approvals(
        conn, plugins_root=plugins, now=_NOW))
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "expired"


def test_plugin_locks_take_the_guard_once_then_the_names_in_order(
        tmp_path, monkeypatch):
    calls = _spy_guard(monkeypatch)
    order = []
    real = switch._plugin_name_lock

    def spy(plugins_root, name, **kwargs):
        order.append(name)
        return real(plugins_root, name, **kwargs)

    monkeypatch.setattr(switch, "_plugin_name_lock", spy)
    with switch._plugin_locks(tmp_path / "plugins", ["gamma", "alpha", "beta",
                                                     "alpha"]):
        pass
    assert calls == [False]
    assert order == ["alpha", "beta", "gamma"]


def test_plugin_locks_reject_an_invalid_name_before_creating_the_guard(
        tmp_path):
    root = tmp_path / "plugins"
    with pytest.raises(ValueError):
        with switch._plugin_locks(root, ["alpha", "../evil"]):
            pass
    assert not switch.deploy_guard_path(root).exists()

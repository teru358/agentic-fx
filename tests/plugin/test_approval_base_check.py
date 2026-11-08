"""承認時の土台照合 (候補が作られた版と、承認時の live の照合)。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import switch
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
CONFIG_YAML = "kind: indicator\noutputs: [v]\n"
TEST_PY_OK = "def test_x():\n    pass\n"


def _fake_pytest_ok(plugin_dir, *, settings):
    from agentic_fx.plugin.gate_pytest import GateResult
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed", duration_sec=0.1)


def _write_candidate_v(dirpath: Path, value: float, name: str | None = None) -> None:
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(
        f"def compute(df, params):\n    return {{'v': {value}}}\n")
    (dirpath / "config.yaml").write_text(CONFIG_YAML)
    (dirpath / "test_plugin.py").write_text(TEST_PY_OK)


@pytest.fixture
def env(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    return plugins_dir, conn, settings


def _submit(env, mission_id: int, value: float, name: str = "sma") -> int:
    plugins_dir, conn, settings = env
    staging = plugins_dir / "_staging" / str(mission_id)
    _write_candidate_v(staging / name, value)
    return switch.submit_candidate(
        conn, name=name, staging_dir=staging, candidate_origin="staging",
        mission_id=mission_id, backlog_id=None, settings=settings, now=NOW)


def _approve(env, approval_id: int):
    plugins_dir, conn, settings = env
    return switch.approve_candidate(
        conn, approval_id, decided_by="h", now=NOW,
        plugins_root=plugins_dir, settings=settings)


def _status(env, approval_id: int) -> str:
    return env[1].execute("SELECT status FROM approval_requests WHERE id=?",
                          (approval_id,)).fetchone()["status"]


def _payload(env, approval_id: int) -> dict:
    return json.loads(env[1].execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["payload_json"])


def _live(env, name: str = "sma") -> str:
    return (env[0] / name).readlink().as_posix()


def _deploy_v1(env) -> str:
    v1 = _submit(env, 1, 1.0)
    _approve(env, v1)
    return _live(env)


def _open_journals(env) -> int:
    return env[1].execute(
        "SELECT COUNT(*) c FROM plugin_switch_journal WHERE phase IN "
        "('preparing','versioned','recorded','switched')").fetchone()["c"]


def test_second_candidate_from_same_base_is_invalidated(env):
    """受入1: 同じ v1 から作った A・B を A → B の順に承認すると B は無効になる。"""
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)
    a_target = _live(env)

    outcome = _approve(env, b)

    assert (outcome.outcome, outcome.status, outcome.reason) == (
        "invalidated", "invalidated", "base_changed")
    assert outcome.op_id is None
    assert _status(env, b) == "invalidated"
    assert env[1].execute("SELECT reason FROM approval_requests WHERE id=?",
                          (b,)).fetchone()["reason"] == "base_changed"
    assert _live(env) == a_target
    assert not (env[0] / "_staging" / "3" / "sma").exists()
    assert _open_journals(env) == 0


def _set_payload(env, approval_id: int, **changes) -> None:
    payload = _payload(env, approval_id)
    for key, value in changes.items():
        if value is _DROP:
            payload.pop(key, None)
        else:
            payload[key] = value
    env[1].execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                   (json.dumps(payload), approval_id))
    env[1].commit()


_DROP = object()


def _versions(env, name: str = "sma") -> list[str]:
    return sorted(p.name for p in (env[0] / ".versions" / name).iterdir())


def test_reverse_order_is_still_superseded_without_reason(env):
    """受入2: B → A の順では A は従来どおり後発の決定で無効になる (reason なし)。"""
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, b)

    outcome = _approve(env, a)

    assert (outcome.outcome, outcome.status, outcome.reason) == (
        "invalidated", "invalidated", None)
    assert env[1].execute("SELECT reason FROM approval_requests WHERE id=?",
                          (a,)).fetchone()["reason"] is None


def test_payload_of_submitted_candidate_records_the_live_version_as_base(env):
    v1_target = _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    assert _payload(env, a)["base"] == {
        "kind": "version", "artifact_hash": v1_target.rsplit("/", 1)[1]}


def test_base_none_passes_when_no_live_and_stops_when_something_is_live(env):
    first = _submit(env, 1, 1.0)
    second = _submit(env, 2, 2.0)
    assert _payload(env, first)["base"] == {"kind": "none"}
    _approve(env, first)

    outcome = _approve(env, second)

    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")


def test_candidate_from_before_disable_stops_but_one_made_after_passes(env):
    """受入4・11: disable で外れた名前は「無し」。停止前の版が土台の候補は止まり、
    停止後に作った (土台なしの) 候補は通る。"""
    from agentic_fx.plugin import deploy_disable
    _deploy_v1(env)
    stale = _submit(env, 2, 2.0)
    deploy_disable.disable_plugin(
        env[1], env[0], "sma", revoke=False, force=False, now=NOW, settings=env[2])
    assert not (env[0] / "sma").exists()
    fresh = _submit(env, 3, 3.0)
    assert _payload(env, fresh)["base"] == {"kind": "none"}

    assert _approve(env, stale).reason == "base_changed"
    assert _approve(env, fresh).outcome == "deployed"


def test_human_submit_takes_live_as_base_and_keeps_human_dir_when_stopped(env):
    """受入5: 人間の提出は提出時の live が土台。止まっても人間の候補は消えない。"""
    plugins_dir, conn, settings = env
    v1_target = _deploy_v1(env)
    other = _submit(env, 2, 2.0)
    human = plugins_dir / "_human"
    _write_candidate_v(human / "sma", 5.0)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=human, candidate_origin="human",
        mission_id=None, backlog_id=None, settings=settings, now=NOW)
    assert _payload(env, approval_id)["base"]["artifact_hash"] == (
        v1_target.rsplit("/", 1)[1])
    _approve(env, other)

    outcome = _approve(env, approval_id)

    assert outcome.reason == "base_changed"
    assert (human / "sma" / "plugin.py").is_file()


def test_human_submit_refuses_unreadable_live(env):
    """受入15: 人間の提出で live が読めない形なら提出を拒否し、行を作らない。"""
    plugins_dir, conn, settings = env
    (plugins_dir / "sma").symlink_to("elsewhere/sma")
    human = plugins_dir / "_human"
    _write_candidate_v(human / "sma", 5.0)
    with pytest.raises(ValueError, match="live_unreadable"):
        switch.submit_candidate(
            conn, name="sma", staging_dir=human, candidate_origin="human",
            mission_id=None, backlog_id=None, settings=settings, now=NOW)
    assert conn.execute("SELECT COUNT(*) c FROM approval_requests").fetchone()["c"] == 0


def _human_submit(env):
    plugins_dir, conn, settings = env
    human = plugins_dir / "_human"
    _write_candidate_v(human / "sma", 5.0)
    return switch.submit_candidate(
        conn, name="sma", staging_dir=human, candidate_origin="human",
        mission_id=None, backlog_id=None, settings=settings, now=NOW)


def _remove_tree(path: Path) -> None:
    import shutil
    for item in [path, *path.rglob("*")]:
        if not item.is_symlink():
            item.chmod(0o700)
    shutil.rmtree(path)


def _break_version_dir(env, how: str) -> None:
    import shutil
    plugins_dir = env[0]
    version_dir = plugins_dir / _live(env)
    if how == "dangling":
        _remove_tree(version_dir)
    elif how == "empty_dir":
        version_dir.chmod(0o700)
        for item in version_dir.iterdir():
            item.chmod(0o600)
            item.unlink()
    elif how == "outside_root":
        outside = plugins_dir.parent / "outside" / "sma"
        outside.parent.mkdir()
        version_dir.chmod(0o700)
        shutil.copytree(plugins_dir / ".versions" / "sma", outside)
        _remove_tree(plugins_dir / ".versions" / "sma")
        (plugins_dir / ".versions" / "sma").symlink_to(outside)


@pytest.mark.parametrize("how", ["dangling", "empty_dir", "outside_root"])
def test_human_submit_refuses_a_live_that_does_not_resolve_to_a_readable_version(env, how):
    """提出時の live は実体まで確かめる: 版 dir の欠損・中身の欠け・root 外への解決は拒否する。"""
    _deploy_v1(env)
    before = env[1].execute("SELECT COUNT(*) c FROM approval_requests").fetchone()["c"]
    _break_version_dir(env, how)
    with pytest.raises(ValueError, match="live_unreadable"):
        _human_submit(env)
    assert env[1].execute(
        "SELECT COUNT(*) c FROM approval_requests").fetchone()["c"] == before


def test_bless_writes_unchecked_base_and_ignores_the_live_version(env):
    """受入5・15: bless は照合しない印を書き、live が何であっても配備できる。"""
    plugins_dir, conn, settings = env
    _deploy_v1(env)
    human = plugins_dir / "_human"
    _write_candidate_v(human / "sma", 9.0)
    approval_id = switch.bless_candidate(
        conn, name="sma", human_dir=human / "sma", settings=settings, now=NOW,
        decided_by="h")
    assert _payload(env, approval_id)["base"] == {"kind": "unchecked", "by": "bless"}
    assert _status(env, approval_id) == "approved"


def _own_journal(env, approval_id: int, phase: str, *, old_target: str,
                 new_target: str, switch_required: bool = True) -> int:
    conn = env[1]
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=approval_id, name="sma",
        old_kind="symlink", old_target=old_target, new_target=new_target,
        switch_required=switch_required, actor="h", now=NOW, commit=True)
    for step in ("versioned", "recorded", "switched"):
        if phase == "preparing":
            break
        switch.advance_switch_journal(conn, op_id, phase=step, now=NOW, commit=True)
        if step == phase:
            break
    return op_id


@pytest.mark.parametrize("phase", ["preparing", "versioned", "recorded"])
def test_stopping_by_base_closes_the_own_unfinished_journal(env, phase):
    """受入6: 自分の未完の journal (switch 前の段階) は照合で止めても残らない。"""
    v1_target = _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)
    op_id = _own_journal(env, b, phase, old_target=v1_target,
                         new_target=".versions/sma/" + "b" * 64)

    outcome = _approve(env, b)

    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")
    assert outcome.rolled_back_op_id == op_id
    assert _open_journals(env) == 0


def test_stopping_by_base_after_rolling_back_a_not_switched_journal(env):
    """受入6: switched だが live が切り替わっていない journal は巻き戻してから止まる。"""
    v0_target = _deploy_v1(env)
    c = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, c)
    c_target = _live(env)
    op_id = _own_journal(env, b, "switched", old_target=c_target,
                         new_target=".versions/sma/" + "b" * 64)
    assert v0_target != c_target

    outcome = _approve(env, b)

    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")
    assert outcome.rolled_back_op_id == op_id
    assert _open_journals(env) == 0
    assert _live(env) == c_target


def test_a_switched_own_journal_resumes_even_when_the_base_differs(env, monkeypatch):
    """受入6: 切替が済んだ自分の journal の再開は照合で止まらない。"""
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)
    original_base = _payload(env, b)["base"]
    _set_payload(env, b, base=_DROP)

    def crash(*args, **kwargs):
        raise RuntimeError("crash before decision")

    with monkeypatch.context() as patch:
        patch.setattr(switch, "_finalize_decision", crash)
        with pytest.raises(RuntimeError):
            _approve(env, b)
    assert _open_journals(env) == 1
    _set_payload(env, b, base=original_base)

    outcome = _approve(env, b)

    assert outcome.outcome == "deployed"
    assert _status(env, b) == "approved"


def test_candidate_from_the_version_a_rollback_returned_to_passes(env):
    """受入7・11: v1 → A → v1 と戻った後の B (v1 土台、A より後の ID) は通る。"""
    from agentic_fx.plugin import deploy_rollback
    v1_target = _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)
    deploy_rollback.rollback_plugin(
        env[1], env[0], "sma", to=v1_target.rsplit("/", 1)[1], revoke=False,
        force=False, now=NOW, settings=env[2])
    assert _live(env) == v1_target

    assert _approve(env, b).outcome == "deployed"


def _make_plain_live(env, value: float) -> None:
    _write_candidate_v(env[0] / "sma", value)


def test_plain_base_defers_to_legacy_plain_then_passes_after_retire(env):
    """受入8: 同じ plain なら既存の legacy_plain_present で pending、retire 後の retry で通る。"""
    from agentic_fx.plugin import deploy_retire
    _make_plain_live(env, 1.0)
    candidate = _submit(env, 2, 2.0)
    assert _payload(env, candidate)["base"]["kind"] == "plain"

    first = _approve(env, candidate)
    assert first.outcome == "legacy_plain_present"
    assert _status(env, candidate) == "pending"

    deploy_retire.retire_plugin(env[1], env[0], "sma", force=False, now=NOW,
                                settings=env[2])
    again = switch.retry_approval(
        env[1], candidate, decided_by="h", now=NOW, plugins_root=env[0],
        settings=env[2])
    assert again.outcome == "deployed"


def test_plain_base_stops_when_the_plain_content_changed(env):
    _make_plain_live(env, 1.0)
    candidate = _submit(env, 2, 2.0)
    (env[0] / "sma" / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 7.0}\n")

    assert _approve(env, candidate).reason == "base_changed"


def test_plain_base_stops_when_live_became_a_version(env):
    _make_plain_live(env, 1.0)
    other = _submit(env, 2, 2.0)
    candidate = _submit(env, 3, 3.0)
    for item in (env[0] / "sma").iterdir():
        item.unlink()
    (env[0] / "sma").rmdir()
    _set_payload(env, other, base={"kind": "none"})
    _approve(env, other)

    assert _approve(env, candidate).reason == "base_changed"


def test_symlink_to_another_hash_stops(env):
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    _set_payload(env, a, base={"kind": "version", "artifact_hash": "c" * 64})

    assert _approve(env, a).reason == "base_changed"


def test_noncanonical_live_target_stops(env):
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    live = env[0] / "sma"
    live.unlink()
    live.symlink_to("../elsewhere/sma")

    assert _approve(env, a).reason == "base_changed"


@pytest.mark.parametrize("broken", [
    {"kind": "mystery"}, {"kind": "version", "artifact_hash": "zz"},
    {"kind": "version"}, {"kind": "plain", "content_hash": 5},
    {"kind": "unchecked", "by": "somebody"}, "text", None, [],
])
def test_broken_base_is_treated_as_a_mismatch_not_as_legacy(env, broken):
    """受入9: キーはあるが形が壊れている依頼は、照合を省かず止める。"""
    a = _submit(env, 1, 1.0)
    _set_payload(env, a, base=broken)

    outcome = _approve(env, a)

    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")


def test_dangling_symlink_with_the_matching_hash_passes(env):
    """受入9: 正しい hash を指すが版 dir が消えている symlink は通る。"""
    import shutil
    v1_target = _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    version_dir = env[0] / v1_target
    version_dir.chmod(0o700)
    for item in version_dir.iterdir():
        item.chmod(0o600)
    shutil.rmtree(version_dir)
    assert (env[0] / "sma").is_symlink() and not (env[0] / "sma").exists()

    assert _approve(env, a).outcome == "deployed"


def test_request_without_base_is_approved_as_before(env):
    """受入9: base の無い依頼 (配備前に作られたもの) は今と同じに通る。"""
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)
    _set_payload(env, b, base=_DROP)

    assert _approve(env, b).outcome == "deployed"


def test_retry_goes_through_the_same_check(env):
    """受入1: retry_approval も同じ照合を受ける。"""
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)

    outcome = switch.retry_approval(
        env[1], b, decided_by="h", now=NOW, plugins_root=env[0], settings=env[2])

    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")


def test_failed_decision_transaction_keeps_staging_and_a_later_try_removes_it(
        env, monkeypatch):
    """受入12: 決定の tx が失敗すると staging は残り、commit の後に消える。"""
    from agentic_fx.store import approvals as approvals_store
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _approve(env, a)
    real = approvals_store.apply_decision

    def failing(*args, **kwargs):
        raise RuntimeError("db fault")

    with monkeypatch.context() as patch:
        patch.setattr(approvals_store, "apply_decision", failing)
        with pytest.raises(RuntimeError):
            _approve(env, b)
    assert (env[0] / "_staging" / "3" / "sma").is_dir()
    assert _status(env, b) == "pending"
    assert approvals_store.apply_decision is real

    assert _approve(env, b).reason == "base_changed"
    assert not (env[0] / "_staging" / "3" / "sma").exists()


def test_invalidation_settles_backlog_with_a_fixed_token_only(env):
    """受入13: 終端の理由は backlog の last_result に固定語だけで届き、版の hash を含まない。"""
    from agentic_fx.store import backlog as backlog_store
    _deploy_v1(env)
    conn = env[1]
    backlog_id = backlog_store.add(conn, "idea", "agent", NOW)
    backlog_store.set_status(conn, backlog_id, "selected", NOW)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _set_payload(env, b, backlog_id=backlog_id)
    _approve(env, a)
    base_hash = _payload(env, b)["base"]["artifact_hash"]

    _approve(env, b)

    row = conn.execute("SELECT status, last_result FROM improvement_backlog WHERE id=?",
                       (backlog_id,)).fetchone()
    assert row["status"] == "observation"
    assert row["last_result"] in ("invalidated", "base_changed")
    assert base_hash not in row["last_result"]


def _submit_with_test_file(env, mission_id: int, value: float, test_py: str,
                           name: str = "sma") -> int:
    plugins_dir, conn, settings = env
    staging = plugins_dir / "_staging" / str(mission_id)
    _write_candidate_v(staging / name, value)
    (staging / name / "test_plugin.py").write_text(test_py)
    return switch.submit_candidate(
        conn, name=name, staging_dir=staging, candidate_origin="staging",
        mission_id=mission_id, backlog_id=None, settings=settings, now=NOW)


def test_an_update_that_changes_only_the_test_file_moves_the_base(env):
    """受入10: content_hash が同じで test_plugin.py だけ違う版の更新も土台を動かす。"""
    v1_target = _deploy_v1(env)
    v1_artifact = v1_target.rsplit("/", 1)[1]
    only_test = _submit_with_test_file(
        env, 2, 1.0, "def test_x():\n    assert True\n")
    other = _submit(env, 3, 3.0)
    assert _payload(env, only_test)["base"]["artifact_hash"] == v1_artifact
    from agentic_fx.plugin import loader
    assert _payload(env, only_test)["content_hash"] == loader.content_hash(env[0] / "sma")

    assert _approve(env, only_test).outcome == "deployed"
    moved = _live(env).rsplit("/", 1)[1]
    assert moved != v1_artifact

    outcome = _approve(env, other)

    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")
    follower = _submit(env, 4, 4.0)
    assert _payload(env, follower)["base"] == {"kind": "version", "artifact_hash": moved}


def _crash_after_switch(env, approval_id: int) -> None:
    def crash(*args, **kwargs):
        raise RuntimeError("crash before decision")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(switch, "_finalize_decision", crash)
        with pytest.raises(RuntimeError):
            _approve(env, approval_id)


def _reconcile(env, **kwargs) -> None:
    switch.reconcile_switch_journals(
        env[1], plugins_root=env[0], now=NOW, settings=env[2], **kwargs)


def test_a_candidate_is_checked_against_the_live_a_reconcile_decided(env):
    """受入11: reconcile が切替済みの A を確定した後の B は、A が動かした live と照合される。"""
    _deploy_v1(env)
    a = _submit(env, 2, 2.0)
    b = _submit(env, 3, 3.0)
    _crash_after_switch(env, a)
    a_target = _live(env)
    assert _open_journals(env) == 1

    _reconcile(env)

    assert _open_journals(env) == 0 and _live(env) == a_target
    outcome = _approve(env, b)
    assert (outcome.outcome, outcome.reason) == ("invalidated", "base_changed")
    assert _live(env) == a_target


def test_approve_and_retry_after_a_reconcile_revert_use_the_restored_live(env):
    """受入11: reconcile が巻き戻した後は、戻った live が土台との照合に使われる。"""
    v1_target = _deploy_v1(env)
    b = _submit(env, 2, 2.0)
    a = _submit(env, 3, 3.0)
    _crash_after_switch(env, a)
    (op_id,) = [r["op_id"] for r in env[1].execute(
        "SELECT op_id FROM plugin_switch_journal WHERE phase IN "
        "('preparing','versioned','recorded','switched')")]

    _reconcile(env, force_revert_op_id=op_id)

    assert _live(env) == v1_target and _status(env, a) == "pending"
    assert _approve(env, b).outcome == "deployed"
    retried = switch.retry_approval(
        env[1], a, decided_by="h", now=NOW, plugins_root=env[0], settings=env[2])
    assert (retried.outcome, retried.reason) == ("invalidated", "base_changed")
    assert _status(env, a) == "invalidated"

"""``afx plugin disable / rollback / retire / revoke / versions`` の CLI 面。

outcome → (rc, code, 文言の要点) の一覧表、確認の流れ、完了表示、lock 待ち、
``versions`` の出力を実 FS + 実 SQLite で確かめる。
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.backtest import plugin_deploy_cli
from agentic_fx.entry import main
from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_retire, deploy_rollback
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store.state import StateStore
from tests.plugin._conc_support import Holder
from tests.plugin.deploy_world import World, at

REPO_ROOT = Path(__file__).resolve().parents[2]


class CliWorld(World):
    """A World laid out like a repo root, so ``afx plugin ...`` finds it from the cwd."""

    def __init__(self, base: Path) -> None:
        (base / "config").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "config" / "settings.yaml.example",
                    base / "config" / "settings.yaml")
        (base / "data" / "state").mkdir(parents=True)
        (base / "logs").mkdir()
        StateStore(base / "data" / "state" / "app_state.json").update(initialized=True)
        self.base = base
        self.db_path = base / "data" / "agentic.db"
        self.conn = db_store.connect(self.db_path)
        db_store.init_db(self.conn)
        self.root = base / "plugins"
        (self.root / "_human").mkdir(parents=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = CliWorld(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: False)
    yield built
    built.conn.close()


def _run(capsys, *argv):
    rc = main(["plugin", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _link_target(world, name="sma") -> str:
    return str((world.root / name).readlink())


# ---- rc / code / 文言の一覧表 ------------------------------------------------

def _disable(outcome, **kw):
    return deploy_disable.DisableOutcome(outcome, "sma", **kw)


def _rollback(outcome, **kw):
    return deploy_rollback.RollbackOutcome(outcome, "sma", **kw)


def _retire(outcome, **kw):
    return deploy_retire.RetireOutcome(outcome, "sma", **kw)


def _revoke(outcome, **kw):
    return deploy_disable.RevokeOutcome(outcome, "sma", "f" * 64, **kw)


HASH = "f" * 64
DEP = (("strat", "fast", "not_found"),)
TARGET = ".versions/sma/" + "b" * 64

OUTCOME_TABLE = [
    # (argv, 差し替える関数, 返す outcome または例外, rc, code, stdout/stderr に含まれる語)
    (["disable", "sma"], deploy_disable, "disable_plugin",
     _disable("disabled", filesystem_switched=True,
              runtime_deactivation="effective_on_next_signal_insert"),
     0, None, ["filesystem_switched=yes",
               "runtime_deactivation=effective_on_next_signal_insert"]),
    (["disable", "sma"], deploy_disable, "disable_plugin", _disable("already_disabled"),
     0, None, ["既に disable 済み"]),
    (["disable", "sma", "--revoke"], deploy_disable, "disable_plugin",
     _disable("revoke_requires_version", version_candidate=HASH),
     2, "revoke_requires_version", [f"afx plugin revoke sma --version {HASH}"]),
    (["disable", "sma"], deploy_disable, "disable_plugin",
     _disable("dependents_blocked", dependents=DEP),
     1, "dependents_blocked", ["strat", "--force"]),
    (["disable", "sma"], deploy_disable, "disable_plugin", _disable("foreign", op_id=3),
     1, "foreign", ["op_id=3", "foreign"]),
    (["disable", "sma"], deploy_disable, "disable_plugin", _disable("needs_retry", op_id=3),
     1, "needs_retry", ["op_id=3", "再実行"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     _rollback("rolled_back", filesystem_switched=True, runtime_activation="pending_restart",
               to_target=TARGET),
     0, None, ["filesystem_switched=yes", "runtime_activation=pending_restart", "再起動後"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     _rollback("refused", reasons=("x is already the live version",),
               refusal_code="already_live"),
     0, None, ["already the live version"]),
    *[(["rollback", "sma"], deploy_rollback, "rollback_plugin",
       _rollback("refused", reasons=(f"reason for {code}",), refusal_code=code),
       1, code, [f"reason for {code}"])
      for code in ("bad_target", "target_not_found", "target_ambiguous", "target_required",
                   "no_rollback_target", "live_is_plain", "target_verification_failed")],
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     _rollback("dependents_blocked", dependents=DEP, refusal_code="dependents_unresolvable"),
     1, "dependents_unresolvable", ["strat", "--force"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     _rollback("verify_failed", reasons=("hash mismatch",),
               refusal_code="target_verification_failed", op_id=4, live_removed=True),
     1, "target_verification_failed",
     ["戻り先の検証に失敗し、元の版 (symlink) は外した。live は不在", "hash mismatch", "op_id=4"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     _rollback("verify_failed", reasons=("hash mismatch",),
               refusal_code="target_verification_failed", op_id=4),
     1, "target_verification_failed", ["戻り先の検証に失敗しました", "op は未完のまま"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin", _rollback("foreign", op_id=5),
     1, "foreign", ["op_id=5"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin", _rollback("needs_retry", op_id=5),
     1, "needs_retry", ["op_id=5", "再実行"]),
    (["retire", "sma"], deploy_retire, "retire_plugin",
     _retire("done", retired_path="_retired/sma-x-1"),
     0, None, ["retire しました", "退避先: _retired/sma-x-1"]),
    (["retire", "sma"], deploy_retire, "retire_plugin", _retire("already_retired"),
     0, None, ["既に retire 済み"]),
    (["retire", "sma"], deploy_retire, "retire_plugin",
     _retire("dependents_blocked", dependents=DEP),
     1, "dependents_blocked", ["strat", "--force"]),
    (["retire", "sma"], deploy_retire, "retire_plugin",
     _retire("refused", reason="cannot use existing destination _retired/sma-x-1"),
     1, "retire_refused", ["refused", "cannot use existing destination _retired/sma-x-1"]),
    (["retire", "sma"], deploy_retire, "retire_plugin", _retire("foreign", op_id=6),
     1, "foreign", ["op_id=6"]),
    (["retire", "sma"], deploy_retire, "retire_plugin",
     _retire("needs_retry", op_id=6, retired_path="_retired/sma-x-1"),
     1, "needs_retry", ["op_id=6", "_retired/sma-x-1"]),
    (["retire", "sma"], deploy_retire, "retire_plugin",
     _disable("disabled", filesystem_switched=True,
              runtime_deactivation="effective_on_next_signal_insert"),
     0, None, ["retire しました", "filesystem_switched=yes"]),
    (["revoke", "sma", "--version", HASH], deploy_disable, "revoke_version",
     _revoke("revoked", op_id=7), 0, None, [f"版 {HASH} を revoke しました"]),
    (["revoke", "sma", "--version", HASH], deploy_disable, "revoke_version",
     _revoke("already_revoked"), 0, None, ["既に revoke 済み"]),
    # 例外
    (["disable", "sma"], deploy_disable, "disable_plugin",
     deploy_ops.SignatureMismatchError("open deploy op op_id=9 has signature (op_kind='x')"),
     1, "signature_mismatch", ["op_id=9", "reconcile", "daemon を再起動"]),
    (["disable", "sma"], deploy_disable, "disable_plugin",
     deploy_ops.DeployOpBlockedError("plugin 'sma' has open deploy op op_id=9"),
     1, "deploy_op_blocked", ["op_id=9", "配備操作が未完です"]),
    (["disable", "sma"], deploy_disable, "disable_plugin",
     plugin_switch.OpenDeployOpError("plugin 'sma' has open deploy op op_id=9 blocking x"),
     1, "deploy_op_blocked", ["op_id=9"]),
    (["disable", "sma"], deploy_disable, "disable_plugin",
     plugin_switch.UnresolvedJournalError("plugin 'sma' journal op_id=9"),
     1, "unresolved_journal", ["op_id=9", "approval retry"]),
    (["disable", "sma"], deploy_disable, "disable_plugin",
     plugin_switch.PluginBusyError("plugin lock deadline exceeded"),
     1, "lock_timeout", ["何も変更していません"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     deploy_ops.DependentsChangedError("dependents changed"),
     1, "dependents_changed", ["再実行"]),
    (["revoke", "sma", "--version", HASH], deploy_disable, "revoke_version",
     ValueError("cannot revoke the current live version; disable it first"),
     1, "invalid_request", ["cannot revoke the current live version"]),
    (["retire", "sma"], deploy_retire, "retire_plugin", OSError("permission denied"),
     1, "os_error", ["permission denied"]),
    (["disable", "sma"], deploy_disable, "disable_plugin",
     sqlite3.OperationalError("database is locked"),
     1, "db_error", ["database is locked"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     RuntimeError("unexpected breakage"),
     1, "internal_error", ["RuntimeError", "unexpected breakage"]),
    (["revoke", "sma", "--version", HASH], deploy_disable, "revoke_version",
     deploy_ops.StateChangedError("live changed"),
     1, "state_changed", ["もう一度実行して予告を確認してください", "何も変更していません"]),
    # dry_run の outcome (確認を飛ばす経路でも表に載せる)
    (["disable", "sma"], deploy_disable, "disable_plugin",
     _disable("dry_run", cascade_strategies=("strat",)),
     0, None, ["dry-run:", "cascade する strategy: strat"]),
    (["rollback", "sma"], deploy_rollback, "rollback_plugin",
     _rollback("dry_run", to_target=TARGET),
     0, None, ["dry-run:", "b" * 12]),
    (["retire", "sma"], deploy_retire, "retire_plugin",
     _retire("dry_run", retired_path="_retired/sma-x-1"),
     0, None, ["dry-run:", "_retired/sma-x-1"]),
    (["revoke", "sma", "--version", HASH], deploy_disable, "revoke_version",
     _revoke("dry_run"), 0, None, ["dry-run:", HASH]),
]


@pytest.mark.parametrize(
    "argv,module,func,result,rc,code,words", OUTCOME_TABLE,
    ids=[f"{row[0][0]}-{getattr(row[3], 'outcome', type(row[3]).__name__)}-{row[5] or 'ok'}"
         for row in OUTCOME_TABLE])
def test_every_outcome_maps_to_one_rc_code_and_message(
        world, monkeypatch, capsys, argv, module, func, result, rc, code, words):
    def fake(*args, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(module, func, fake)

    got_rc, out, err = _run(capsys, *argv, "--yes")

    assert got_rc == rc
    if code is None:
        assert err == ""
    else:
        assert err.splitlines()[0].startswith("エラー: ")
        assert err.splitlines()[0].endswith(f"(code={code})")
    for word in words:
        assert word in out + err, (word, out, err)


def test_rollback_success_never_reports_a_bare_completion(world, capsys):
    _a, b, c = world.three_generations()

    rc, out, err = _run(capsys, "rollback", "sma", "--yes")

    assert rc == 0 and err == ""
    assert _link_target(world) == world.target("sma", b)
    assert "filesystem_switched=yes runtime_activation=pending_restart" in out
    assert out.strip() != "rollback 完了"
    assert "rollback 完了" not in out


# ---- 確認 (TTY / --yes / --dry-run) -------------------------------------------

def _tty(monkeypatch, answer: str | None):
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: True)
    if answer is None:
        def refuse(_prompt=""):
            raise AssertionError("must not ask")
        monkeypatch.setattr("builtins.input", refuse)
    else:
        monkeypatch.setattr("builtins.input", lambda _prompt="": answer)


def test_without_a_terminal_or_yes_nothing_changes_and_confirmation_is_required(world, capsys):
    _a, _b, c = world.three_generations()
    before = world.snapshot("sma")

    rc, out, err = _run(capsys, "rollback", "sma")

    assert rc == 1
    assert err.splitlines()[0].endswith("(code=confirmation_required)")
    assert "runtime_activation=pending_restart" in out
    assert world.snapshot("sma") == before


def test_declining_the_terminal_prompt_aborts_without_changes(world, monkeypatch, capsys):
    world.three_generations()
    before = world.snapshot("sma")
    _tty(monkeypatch, "n")

    rc, out, err = _run(capsys, "disable", "sma")

    assert rc == 1
    assert err.splitlines()[0].endswith("(code=aborted)")
    assert "cascade する strategy" in out
    assert world.snapshot("sma") == before


def test_eof_at_the_prompt_is_a_refusal(world, monkeypatch, capsys):
    world.three_generations()
    before = world.snapshot("sma")
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: True)

    def eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    rc, _out, err = _run(capsys, "disable", "sma")

    assert rc == 1 and err.splitlines()[0].endswith("(code=aborted)")
    assert world.snapshot("sma") == before


def test_accepting_the_terminal_prompt_runs_the_operation(world, monkeypatch, capsys):
    world.three_generations()
    _tty(monkeypatch, "y")

    rc, out, _err = _run(capsys, "disable", "sma")

    assert rc == 0
    assert not (world.root / "sma").is_symlink()
    assert "filesystem_switched=yes runtime_deactivation=effective_on_next_signal_insert" in out


def test_yes_skips_the_prompt(world, monkeypatch, capsys):
    world.three_generations()
    _tty(monkeypatch, None)

    rc, _out, _err = _run(capsys, "disable", "sma", "--yes")

    assert rc == 0 and not (world.root / "sma").is_symlink()


def test_dry_run_shows_the_plan_and_changes_nothing(world, capsys):
    _a, b, c = world.three_generations()
    before = world.snapshot("sma")

    rc, out, err = _run(capsys, "rollback", "sma", "--revoke", "--dry-run")

    assert rc == 0 and err == ""
    assert out.startswith("dry-run:")
    assert b.name[:12] in out
    assert world.content(c) in out
    assert "runtime_activation=pending_restart" in out
    assert world.snapshot("sma") == before


def test_the_preview_names_cascading_strategies_and_the_revoked_version(world, capsys):
    indicator = world.version("ind", "i")
    strategy = world.version("strat", "s", kind="strategy", indicator="ind")
    world.deploy("ind", indicator, None, 1)
    world.deploy("strat", strategy, None, 2)

    rc, out, err = _run(capsys, "disable", "ind", "--force", "--revoke", "--dry-run")

    assert rc == 0, err
    assert "cascade する strategy: strat" in out
    assert f"版 {world.content(indicator)}" in out


def test_a_noop_needs_no_confirmation(world, capsys):
    world.three_generations()
    _run(capsys, "disable", "sma", "--yes")

    rc, out, err = _run(capsys, "disable", "sma")

    assert rc == 0 and "既に disable 済み" in out and err == ""


def test_revoke_asks_first_and_records_the_version_only_after_yes(world, capsys):
    a, _b, _c = world.three_generations()
    content = world.content(a)

    rc, out, err = _run(capsys, "revoke", "sma", "--version", content)
    assert rc == 1 and err.splitlines()[0].endswith("(code=confirmation_required)")
    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 0
    assert "FS は変更しません" in out

    rc, out, err = _run(capsys, "revoke", "sma", "--version", content[:12], "--yes")
    assert rc == 0, err
    assert f"版 {content} を revoke しました" in out
    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 1

    rc, out, _err = _run(capsys, "revoke", "sma", "--version", content)
    assert rc == 0 and "既に revoke 済み" in out


def test_revoke_of_the_live_version_is_refused_with_a_code(world, capsys):
    _a, _b, c = world.three_generations()

    rc, _out, err = _run(capsys, "revoke", "sma", "--version", world.content(c), "--yes")

    assert rc == 1
    assert err.splitlines()[0].endswith("(code=invalid_request)")
    assert "disable it first" in err


def test_revoke_prefix_that_matches_no_version_is_refused(world, capsys):
    world.three_generations()

    rc, _out, err = _run(capsys, "revoke", "sma", "--version", "0123456789ab", "--yes")

    assert rc == 1 and err.splitlines()[0].endswith("(code=target_not_found)")


def test_disable_revoke_without_a_live_points_to_revoke_with_rc_2(world, capsys):
    a, b, c = world.three_generations()
    _run(capsys, "disable", "sma", "--yes")

    rc, _out, err = _run(capsys, "disable", "sma", "--revoke", "--yes")

    assert rc == 2
    assert err.splitlines()[0].endswith("(code=revoke_requires_version)")
    assert f"afx plugin revoke sma --version {world.content(c)}" in err


def test_retire_of_a_plain_directory_reports_the_destination(world, capsys):
    version = world.version("old", "p")
    shutil.copytree(version, world.root / "old")

    rc, out, err = _run(capsys, "retire", "old", "--yes")

    assert rc == 0, err
    assert "退避先: _retired/old-" in out
    assert not (world.root / "old").exists()


def test_a_rerun_with_a_different_signature_shows_the_op_and_the_wait_hint(world, capsys):
    _a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c)
    (world.root / "sma").unlink()

    rc, _out, err = _run(capsys, "disable", "sma", "--revoke", "--yes")

    assert rc == 1
    first = err.splitlines()[0]
    assert first.endswith("(code=signature_mismatch)")
    assert "op_id=" in err
    assert "reconcile の完了を待つか daemon を再起動" in err


def test_a_stuck_syscall_failure_is_reported_as_open_and_resumes_on_rerun(
        world, monkeypatch, capsys):
    world.three_generations()
    real_unlink = deploy_disable.os.unlink
    state = {"fail": True}

    def flaky(path):
        if state["fail"]:
            raise PermissionError("blocked")
        return real_unlink(path)

    monkeypatch.setattr(deploy_disable.os, "unlink", flaky)
    rc, _out, err = _run(capsys, "disable", "sma", "--yes")
    assert rc == 1
    assert err.splitlines()[0].endswith("(code=needs_retry)")
    assert "fail_reason=syscall_failed" in err

    state["fail"] = False
    rc, out, _err = _run(capsys, "disable", "sma", "--yes")
    assert rc == 0 and "filesystem_switched=yes" in out
    assert deploy_store.get_open_by_name(world.conn, "sma") is None


# ---- 予告と本実行の間の状態変化 -------------------------------------------

def _change_while_prompting(monkeypatch, action, answer: str = "y") -> dict:
    """Answer the prompt, but first run ``action`` as if another process acted meanwhile."""
    seen: dict = {}
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: True)

    def ask(_prompt=""):
        action()
        seen["after_change"] = True
        return answer

    monkeypatch.setattr("builtins.input", ask)
    return seen


def _assert_refused_as_state_changed(rc, err, world, expected_snapshot):
    assert rc == 1
    assert err.splitlines()[0].endswith("(code=state_changed)")
    assert "もう一度実行して予告を確認してください" in err
    assert world.snapshot("sma") == expected_snapshot


def test_disable_with_revoke_does_not_touch_a_version_swapped_in_after_the_preview(
        world, monkeypatch, capsys):
    _a, b, c = world.three_generations()
    snapshots: dict = {}

    def swap():
        world.link("sma", b)
        snapshots["after"] = world.snapshot("sma")

    _change_while_prompting(monkeypatch, swap)

    rc, out, err = _run(capsys, "disable", "sma", "--revoke")

    assert "版 " + world.content(c) in out
    _assert_refused_as_state_changed(rc, err, world, snapshots["after"])
    assert _link_target(world) == world.target("sma", b)
    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 0


def test_rollback_does_not_run_when_the_live_version_moved_after_the_preview(
        world, monkeypatch, capsys):
    a, b, c = world.three_generations()
    snapshots: dict = {}

    def swap():
        world.link("sma", b)
        snapshots["after"] = world.snapshot("sma")

    _change_while_prompting(monkeypatch, swap)

    rc, _out, err = _run(capsys, "rollback", "sma", "--to", a.name[:12])

    _assert_refused_as_state_changed(rc, err, world, snapshots["after"])
    assert _link_target(world) == world.target("sma", b)


def test_retire_does_not_run_when_the_plain_directory_changed_after_the_preview(
        world, monkeypatch, capsys):
    shutil.copytree(world.version("old", "p"), world.root / "old")
    snapshots: dict = {}

    def edit():
        (world.root / "old" / "plugin.py").write_text("# edited meanwhile\n")
        snapshots["after"] = world.snapshot("old")

    _change_while_prompting(monkeypatch, edit)

    rc, _out, err = _run(capsys, "retire", "old")

    assert rc == 1 and err.splitlines()[0].endswith("(code=state_changed)")
    assert world.snapshot("old") == snapshots["after"]
    assert (world.root / "old").is_dir()


def test_revoke_does_not_run_when_the_latest_decision_changed_after_the_preview(
        world, monkeypatch, capsys):
    from agentic_fx.store import approvals as approvals_store

    a, _b, _c = world.three_generations()
    snapshots: dict = {}

    def revoke_elsewhere():
        approvals_store.append_revocation(world.conn, "sma", world.content(a), now=at(30))
        snapshots["after"] = world.snapshot("sma")

    _change_while_prompting(monkeypatch, revoke_elsewhere)

    rc, _out, err = _run(capsys, "revoke", "sma", "--version", world.content(a))

    _assert_refused_as_state_changed(rc, err, world, snapshots["after"])
    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 1


def test_a_noop_preview_never_reaches_the_real_run_without_yes(world, monkeypatch, capsys):
    _a, _b, c = world.three_generations()
    _run(capsys, "disable", "sma", "--yes")
    real = deploy_disable.disable_plugin
    calls: list[bool] = []

    def preview_then_live_appears(*args, **kwargs):
        calls.append(kwargs["dry_run"])
        outcome = real(*args, **kwargs)
        if kwargs["dry_run"]:
            world.link("sma", c)
        return outcome

    monkeypatch.setattr(deploy_disable, "disable_plugin", preview_then_live_appears)

    rc, _out, err = _run(capsys, "disable", "sma")

    assert calls == [True]
    assert rc == 1 and err.splitlines()[0].endswith("(code=state_changed)")
    assert _link_target(world) == world.target("sma", c)
    assert deploy_store.get_open_by_name(world.conn, "sma") is None
    assert len(deploy_store.list_by_name(world.conn, "sma")) == 1


def test_a_noop_without_yes_still_resends_a_missed_notification(world, capsys):
    _a, _b, c = world.three_generations()
    (world.root / "sma").unlink()
    op_id = world.intent("sma", op_kind="disable_symlink", phase="done", from_ver=c, minute=10)

    rc, out, err = _run(capsys, "disable", "sma")

    assert rc == 0 and err == ""
    assert "既に disable 済み" in out
    assert f"op_id={op_id}" in out
    assert deploy_store.get_by_id(world.conn, op_id)["notify_attempted"] == 1


# ---- lock 待ち ----------------------------------------------------------

def _run_in_thread(argv, capsys_box):
    def target():
        capsys_box["rc"] = main(["plugin", *argv])

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_the_cli_waits_for_another_process_holding_the_name_lock_and_then_completes(
        world, monkeypatch, capsys, tmp_path):
    world.three_generations()
    _tty(monkeypatch, None)
    waiting = threading.Event()
    real_set = plugin_switch.set_plugin_lock_wait_notice

    def spy(notice):
        if notice is not None:
            def announce():
                notice()
                waiting.set()
            return real_set(announce)
        return real_set(None)

    monkeypatch.setattr(plugin_switch, "set_plugin_lock_wait_notice", spy)
    holder = Holder(world.root, tmp_path / "barrier", ["sma"])
    box: dict = {}
    try:
        thread = _run_in_thread(["disable", "sma", "--yes"], box)
        assert waiting.wait(20), "the CLI never reached the held lock"
        assert thread.is_alive() and "rc" not in box
        assert (world.root / "sma").is_symlink()
        holder.release()
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        holder.close()
    assert box["rc"] == 0
    assert not (world.root / "sma").is_symlink()
    assert "他の操作の完了を待っています (最大 30 秒)" in capsys.readouterr().err


def test_a_lock_held_past_the_deadline_is_a_lock_timeout_that_changes_nothing(
        world, monkeypatch, capsys, tmp_path):
    world.three_generations()
    before = world.snapshot("sma")
    monkeypatch.setattr(plugin_deploy_cli, "LOCK_WAIT_SECONDS", 0.3)
    holder = Holder(world.root, tmp_path / "barrier", ["sma"])
    try:
        rc, _out, err = _run(capsys, "rollback", "sma", "--yes")
    finally:
        holder.release()
    assert rc == 1
    assert err.splitlines()[0].endswith("(code=lock_timeout)")
    assert world.snapshot("sma") == before


# ---- versions ----------------------------------------------------------

def _versions_scenario(world):
    a, b, c = world.three_generations()
    deploy_disable.revoke_version(
        world.conn, world.root, "sma", content_hash=world.content(a), now=at(10))
    world.intent("sma", op_kind="rollback_symlink", phase="needs_retry",
                 fail_reason="verify_failed", from_ver=c, to_ver=b, minute=20, notify=True)
    world.intent("sma", op_kind="disable_symlink", phase="foreign", from_ver=c, minute=21)
    world.intent("sma", op_kind="rollback_symlink", phase="intent", from_ver=c, to_ver=b,
                 minute=22)
    return a, b, c


def test_versions_prints_live_versions_history_ops_and_dependents_exactly(world, capsys):
    a, b, c = _versions_scenario(world)

    rc, out, err = _run(capsys, "versions", "sma")

    def v(path):
        return path.name[:12]

    def h(path):
        return world.content(path)[:12]

    assert rc == 0 and err == ""
    assert out == "\n".join([
        "plugin sma",
        "-- live --",
        f"symlink -> .versions/sma/{c.name} (version {v(c)})",
        "-- 版 (先頭 = `rollback --to` に使う版、content = `revoke --version` に使う識別) --",
        f"* {v(c)}  content={h(c)}  決定=approved  承認=2026-10-16T03:03:00+00:00 by human"
        "  版dir=整合",
        f"  {v(a)}  content={h(a)}  決定=rejected  承認=2026-10-16T03:01:00+00:00 by human"
        "  却下=2026-10-16T03:10:00+00:00 by human_cli  版dir=整合",
        f"  {v(b)}  content={h(b)}  決定=approved  承認=2026-10-16T03:02:00+00:00 by human"
        "  版dir=整合",
        "-- 経歴 (live の変遷) --",
        f"2026-10-16T03:01:00+00:00  approve  absent -> {v(a)}",
        f"2026-10-16T03:02:00+00:00  approve  {v(a)} -> {v(b)}",
        f"2026-10-16T03:03:00+00:00  approve  {v(b)} -> {v(c)}",
        f"2026-10-16T03:20:00.000001+00:00  rollback_symlink  {v(c)} -> absent",
        "-- deploy op --",
        f"未完 op_id=4 rollback_symlink intent updated_at=2026-10-16T03:22:00+00:00 "
        f"subject={h(c)} to={v(b)} 通知試行済み=いいえ",
        "  同じコマンドの再実行で続きから完遂します。進まなければ daemon を再起動"
        " (起動時の reconcile) するか reconcile を待ってください",
        f"要注意 op_id=2 rollback_symlink needs_retry/verify_failed "
        f"updated_at=2026-10-16T03:20:00.000001+00:00 subject={h(c)} to={v(b)} 通知試行済み=はい",
        f"要注意 op_id=3 disable_symlink foreign updated_at=2026-10-16T03:21:00.000001+00:00 "
        f"subject={h(c)} to=- 通知試行済み=いいえ",
        "-- 依存 strategy --",
        "(なし)",
        "",
    ])


def test_versions_marks_an_in_place_edited_version_directory_as_inconsistent(world, capsys):
    _a, b, _c = world.three_generations()
    (b / "plugin.py").write_text("# edited after approval\n")

    rc, out, _err = _run(capsys, "versions", "sma")

    assert rc == 0
    line = next(line for line in out.splitlines() if line.strip().startswith(b.name[:12]))
    assert "版dir=不整合" in line


def test_versions_names_dependent_strategies(world, capsys):
    indicator = world.version("ind", "i")
    strategy = world.version("strat", "s", kind="strategy", indicator="ind", pin=None)
    world.deploy("ind", indicator, None, 1)
    world.deploy("strat", strategy, None, 2)

    rc, out, _err = _run(capsys, "versions", "ind")

    assert rc == 0
    assert "-- 依存 strategy --" in out and "strat" in out.split("-- 依存 strategy --")[1]


def test_versions_shows_a_missing_live_and_no_ops_plainly(world, capsys):
    world.three_generations()
    _run(capsys, "disable", "sma", "--yes")

    rc, out, _err = _run(capsys, "versions", "sma")

    assert rc == 0
    assert "-- live --\n不在\n" in out
    assert "(未完・要注意の op はありません)" in out
    history = out.split("-- 経歴 (live の変遷) --")[1].split("-- deploy op --")[0]
    assert "disable_symlink" in history and history.rstrip().endswith("-> absent")


def test_versions_shows_a_plain_live(world, capsys):
    shutil.copytree(world.version("old", "p"), world.root / "old")

    rc, out, _err = _run(capsys, "versions", "old")

    assert rc == 0 and "plain directory" in out and "retire" in out


def test_versions_reads_without_the_name_lock_and_writes_nothing(world, capsys, tmp_path):
    _versions_scenario(world)
    before = world.snapshot("sma")
    holder = Holder(world.root, tmp_path / "barrier", ["sma"])
    box: dict = {}
    try:
        thread = _run_in_thread(["versions", "sma"], box)
        thread.join(timeout=20)
        assert not thread.is_alive(), "versions waited for the lock"
    finally:
        holder.release()
    assert box["rc"] == 0
    assert world.snapshot("sma") == before


def _tree(base: Path):
    return sorted((str(path.relative_to(base)), path.stat().st_size if path.is_file() else -1)
                  for path in base.rglob("*"))


def _bare_root(base: Path) -> Path:
    (base / "config").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "config" / "settings.yaml.example",
                base / "config" / "settings.yaml")
    (base / "data" / "state").mkdir(parents=True)
    StateStore(base / "data" / "state" / "app_state.json").update(initialized=True)
    return base


def test_versions_on_a_root_without_a_database_creates_nothing_and_says_so(
        tmp_path, monkeypatch, capsys):
    base = _bare_root(tmp_path / "bare")
    monkeypatch.chdir(base)
    before = _tree(base)

    rc, _out, err = _run(capsys, "versions", "sma")

    assert rc == 1
    assert err.splitlines()[0].endswith("(code=db_missing)")
    assert "初期化" in err
    assert _tree(base) == before
    assert not (base / "data" / "agentic.db").exists()
    assert not (base / "plugins").exists()


def test_versions_on_an_existing_database_leaves_every_file_untouched(world, capsys):
    _versions_scenario(world)
    before = _tree(world.base)
    db_bytes = world.db_path.read_bytes()

    rc, _out, _err = _run(capsys, "versions", "sma")

    assert rc == 0
    assert _tree(world.base) == before
    assert world.db_path.read_bytes() == db_bytes


def test_versions_opens_the_database_read_only(world, monkeypatch, capsys):
    import sqlite3

    _versions_scenario(world)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)

    rc, _out, _err = _run(capsys, "versions", "sma")

    assert rc == 0
    assert opened and all("mode=ro" in target for target in opened), opened


def test_versions_takes_the_latest_decision_from_the_approvals_store(world, monkeypatch, capsys):
    from agentic_fx.store import approvals as approvals_store

    a, _b, c = _versions_scenario(world)
    asked: list[str] = []
    real = approvals_store.latest_decision_for_identity

    def spy(conn, name, content_hash):
        asked.append(content_hash)
        return real(conn, name, content_hash)

    monkeypatch.setattr(approvals_store, "latest_decision_for_identity", spy)

    rc, out, _err = _run(capsys, "versions", "sma")

    assert rc == 0
    assert {world.content(a), world.content(c)} <= set(asked)
    assert "決定=rejected" in out


def test_versions_rejects_an_invalid_plugin_name_with_a_code(world, capsys):
    rc, _out, err = _run(capsys, "versions", "../evil")

    assert rc == 1 and err.splitlines()[0].endswith("(code=invalid_request)")


# ---- 通知の失敗 ----------------------------------------------------------

def test_a_failing_activity_write_does_not_undo_a_completed_operation(world, monkeypatch, capsys):
    from agentic_fx.activity import ActivityLog

    world.three_generations()

    def broken(self, *args, **kwargs):
        raise OSError("activity log is not writable")

    monkeypatch.setattr(ActivityLog, "write", broken)

    rc, out, err = _run(capsys, "rollback", "sma", "--yes")

    assert rc == 0 and err == ""
    row = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert row["op_kind"] == "rollback_symlink"
    assert row["phase"] == "done" and row["notify_attempted"] == 1
    assert "runtime_activation=pending_restart" in out

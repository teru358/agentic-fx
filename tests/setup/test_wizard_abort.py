from __future__ import annotations

import contextlib
import subprocess
import sys

from agentic_fx.setup.wizard import NON_TTY_MESSAGE, run_wizard

from tests.setup.wizard_support import (
    LOCK_FILES, ScriptedWizardIO, fresh_responder, make_deps, make_root, snapshot,
)

_HOLDER = """
import sys
from pathlib import Path
root = Path(sys.argv[1])
if sys.argv[2] == "instance":
    from agentic_fx.store.instance_lock import acquire_instance_lock
    held = acquire_instance_lock(root / "data")
else:
    from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
    held = acquire_setup_lease(root)
print("ready", flush=True)
sys.stdin.readline()
"""


@contextlib.contextmanager
def holding(root, kind):
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(root), kind], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        yield
    finally:
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=30)


def only_locks_added(before, after):
    assert set(after) - set(before) <= LOCK_FILES
    assert {path: digest for path, digest in after.items() if path in before} == before


def test_ctrl_c_eof_or_q_at_any_question_before_the_commit_leaves_only_lock_files(tmp_path):
    probe_root = make_root(tmp_path / "probe")
    deps, *_ = make_deps(tmp_path / "probe")
    probe = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(probe_root, probe, deps) == 0
    last = max(i for i, prompt in enumerate(probe.asked) if "書き込みますか" in prompt)
    assert last > 15
    for index in range(last + 1):
        for kind, expected in (("ctrl-c", 130), ("eof", 1), ("q", 1)):
            case = tmp_path / f"{kind}{index}"
            root = make_root(case)
            before = snapshot(root)
            deps, *_ = make_deps(case)
            if kind == "q":
                rules = fresh_responder()
                io = ScriptedWizardIO(responder=lambda prompt, n=[0], i=index: (
                    "q" if (n.__setitem__(0, n[0] + 1) or n[0] - 1 == i) else rules(prompt)))
            else:
                error = KeyboardInterrupt() if kind == "ctrl-c" else EOFError()
                io = ScriptedWizardIO(responder=fresh_responder(), raise_at=(index, error))
            assert run_wizard(root, io, deps) == expected, (kind, index, io.text[-300:])
            only_locks_added(before, snapshot(root))
            assert "何も変えていません" in io.text


def test_declining_the_final_confirmation_writes_nothing(tmp_path):
    root = make_root(tmp_path)
    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("書き込みますか", "n")))
    assert run_wizard(root, io, deps) == 1
    only_locks_added(before, snapshot(root))


def test_a_missing_example_stops_before_anything_is_created(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 2
    assert "settings.yaml.example" in io.text
    assert snapshot(root) == {}


def test_a_non_terminal_stops_before_any_lock_file_is_created(tmp_path):
    root = make_root(tmp_path)
    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(tty=False)
    assert run_wizard(root, io, deps) == 2
    assert NON_TTY_MESSAGE in io.text and "--non-interactive" in io.text
    assert snapshot(root) == before


def test_another_running_instance_stops_the_wizard_without_changing_anything(tmp_path):
    root = make_root(tmp_path)
    (root / "data").mkdir()
    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    with holding(root, "instance"):
        io = ScriptedWizardIO(responder=fresh_responder())
        assert run_wizard(root, io, deps) == 2
    assert "別の agentic-fx が動いています" in io.text
    only_locks_added(before, snapshot(root))
    assert not (root / "config" / "settings.yaml").exists()


def test_another_setup_operation_stops_the_wizard_before_it_touches_the_instance_lock(tmp_path):
    root = make_root(tmp_path)
    hooks: list[str] = []
    deps, *_ = make_deps(tmp_path, hook=hooks.append)
    with holding(root, "setup"):
        io = ScriptedWizardIO(responder=fresh_responder())
        assert run_wizard(root, io, deps) == 2
    assert "別の `afx init` または `afx ops enable` が実行中です" in io.text
    assert "before_instance_lock_acquire" not in hooks
    assert not (root / "config" / "settings.yaml").exists()


def test_the_setup_lock_is_taken_before_the_instance_lock(tmp_path):
    root = make_root(tmp_path)
    hooks: list[str] = []
    deps, *_ = make_deps(tmp_path, hook=hooks.append)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    order = [h for h in hooks if h.endswith("lock_acquire")]
    assert order == ["before_setup_lock_acquire", "after_setup_lock_acquire",
                     "before_instance_lock_acquire", "after_instance_lock_acquire"]


def test_an_unfinished_ops_enable_stops_the_wizard(tmp_path):
    root = make_root(tmp_path)
    (root / "data" / "ops").mkdir(parents=True)
    (root / "data" / "ops" / "enroll_in_progress").write_text("x")
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 2
    assert "afx ops enable <feature>" in io.text and "途中で止まっています" in io.text
    assert not (root / "config" / "settings.yaml").exists()


def test_both_markers_report_the_ops_enable_first(tmp_path):
    root = make_root(tmp_path)
    (root / "data" / "ops").mkdir(parents=True)
    (root / "data" / "ops" / "enroll_in_progress").write_text("x")
    (root / "data" / "state").mkdir()
    (root / "data" / "state" / "setup_in_progress").write_bytes(b"phase=handoff\n")
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 2
    assert "afx ops enable" in io.text and "前回の初期設定" not in io.text


def test_without_landlock_the_wizard_asks_to_continue_and_skips_improvement_questions(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, landlock_available=lambda: False)
    io = ScriptedWizardIO(responder=fresh_responder(("続けますか", "n")))
    before = snapshot(root)
    assert run_wizard(root, io, deps) == 1
    only_locks_added(before, snapshot(root))

    root2 = make_root(tmp_path / "second")
    deps2, *_ = make_deps(tmp_path / "second", landlock_available=lambda: False)
    io2 = ScriptedWizardIO(responder=fresh_responder(("続けますか", "y")))
    assert run_wizard(root2, io2, deps2) == 0, io2.text
    assert io2.asked_with("改善の周期") == [] and io2.asked_with("最初の 1 本") == []
    assert "Landlock が使えません" in io2.text and "標準指標の導入は飛ばします" in io2.text


def test_a_missing_pytest_is_only_a_warning(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, find_spec=lambda name: None)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    assert "pytest を使います" in io.text


def test_an_unsafe_lock_directory_stops_with_a_permission_message(tmp_path):
    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "data").chmod(0o777)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 1
    assert "権限が安全ではありません" in io.text
    assert not (root / "config" / "settings.yaml").exists()


def test_declining_on_a_rerun_with_an_existing_database_leaves_every_file_untouched(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    before = snapshot(root)
    assert "data/agentic.db" in before
    deps2, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("口座通貨", "USD"), ("'yes' と打ち込", "yes"), ("書き込みますか", "n")))
    assert run_wizard(root, io, deps2) == 1
    assert "建玉・未約定" not in io.text and "何も書かずに終了します" in io.text
    after = snapshot(root)
    assert after == before

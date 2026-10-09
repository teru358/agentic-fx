"""AC-20 / 28 / 53: approver 操作は stdin と /dev/tty の両方が端末のときだけ送る。

端末は実 process + pty で作る。stdin だけ pipe に差し替えた子は /dev/tty を持ったまま
になるので、「/dev/tty があっても stdin が pipe なら送らない」を直接確かめられる。
"""
from __future__ import annotations

import resource
import subprocess
import sys
from pathlib import Path

import pytest

from ._ctl import ctl_api, run_ctl, run_pty  # noqa: F401

CRASH_CHILD = Path(__file__).with_name("_reset_crash_child.py")
APPROVER = ["ctl", "--as", "approver"]


def _approve_rows(env):
    return [row for row in env.ops_rows()
            if row["endpoint"] == "POST /v1/approvals/{id}/approve"]


# ---------------------------------------------------------------- AC-20

def test_ac20_approve_with_piped_stdin_is_not_sent_even_with_dev_tty(ctl_api):
    env = ctl_api.env
    approval_id = env.make_plugin_approval()
    result = run_pty(env.root, APPROVER + ["approve", str(approval_id)], [],
                     stdin_pipe=f"{approval_id}\n")
    assert result.rc == 2, result.output
    assert "端末からの確認が要ります" in result.output
    assert env.approval(approval_id)["status"] == "pending"
    assert env.ops_rows() == []


def test_ac20_approve_on_terminal_sends_only_after_the_id_is_retyped(ctl_api):
    env = ctl_api.env
    approval_id = env.make_plugin_approval()
    result = run_pty(env.root, APPROVER + ["approve", str(approval_id)],
                     [("もう一度入力してください", str(approval_id))])
    assert result.rc == 0, result.output
    # 親記録と自己申告を表示してから id を求めた
    shown = result.output.split("もう一度入力してください")[0]
    assert f"approval #{approval_id} kind=plugin status=pending" in shown
    assert "--- 親が受理・記録した内容 ---" in shown
    assert "payload_sha256=" in shown
    assert f"approval #{approval_id} approved" in result.output
    row = env.approval(approval_id)
    assert (row["status"], row["decided_by"]) == ("approved", "api:approver")


def test_ac20_approve_with_wrong_id_is_not_sent(ctl_api):
    env = ctl_api.env
    approval_id = env.make_plugin_approval()
    result = run_pty(env.root, APPROVER + ["approve", str(approval_id)],
                     [("もう一度入力してください", str(approval_id + 1))])
    assert result.rc == 1, result.output
    assert "取り消しました" in result.output
    assert env.approval(approval_id)["status"] == "pending"
    assert _approve_rows(env) == []


@pytest.mark.parametrize("argv", [
    ["reject", "{id}"], ["approval", "retry", "{id}"], ["data", "resume"],
    ["killswitch", "reset", "1"], ["killswitch", "reconcile"],
    ["jobs", "cancel", "0" * 32]])
def test_ac20_confirmed_operations_refuse_non_terminal_stdin(ctl_api, argv):
    env = ctl_api.env
    approval_id = env.raw_approval()
    env.state.update(kill_switch_latched=True)
    tokens = [a.format(id=approval_id) for a in argv]
    rc, out, err = run_ctl(env.root, ["--as", "approver"] + tokens)
    assert (rc, out) == (2, "")
    assert "端末からの確認が要ります" in err
    assert env.ops_rows() == []
    assert env.approval(approval_id)["status"] == "pending"
    assert env.state.load().kill_switch_latched is True


def test_ac20_reject_on_terminal_asks_yes_no(ctl_api):
    env = ctl_api.env
    approval_id = env.make_plugin_approval()
    declined = run_pty(env.root, APPROVER + ["reject", str(approval_id), "理由"],
                       [("[y/N]", "")])
    assert declined.rc == 1, declined.output
    assert env.approval(approval_id)["status"] == "pending"
    accepted = run_pty(env.root, APPROVER + ["reject", str(approval_id), "理由"],
                       [("[y/N]", "y")])
    assert accepted.rc == 0, accepted.output
    assert f"approval #{approval_id} rejected" in accepted.output
    assert env.approval(approval_id)["status"] == "rejected"


def test_ac20_killswitch_reset_requires_typing_reset(ctl_api):
    env = ctl_api.env
    generation = env.state.update(kill_switch_latched=True).kill_switch_generation
    argv = APPROVER + ["killswitch", "reset", str(generation)]
    declined = run_pty(env.root, argv, [("reset と入力してください", "y")])
    assert declined.rc == 1, declined.output
    # 世代・発動時刻・equity を表示してから求める
    assert f"gen={generation}" in declined.output and "発動時刻=" in declined.output
    assert "エクイティ=" in declined.output
    assert env.state.load().kill_switch_latched is True
    accepted = run_pty(env.root, argv, [("reset と入力してください", "reset")])
    assert accepted.rc == 0, accepted.output
    assert env.state.load().kill_switch_latched is False


# ---------------------------------------------------------------- AC-28

def test_ac28_interactive_mode_confirms_every_approver_operation(ctl_api):
    env = ctl_api.env
    first = env.make_plugin_approval()
    second = env.make_plugin_approval(name="ema", mission_id=2)
    result = run_pty(env.root, APPROVER, [
        ("afx ctl> ", f"reject {first}"), ("[y/N]", "y"),
        ("afx ctl> ", f"reject {second}"), ("[y/N]", "n"),
        ("afx ctl> ", f"reject {second}"), ("[y/N]", "y"),
        ("afx ctl> ", "exit")])
    assert result.rc == 0, result.output
    assert result.output.count("[y/N]") == 3
    assert env.approval(first)["status"] == "rejected"
    assert env.approval(second)["status"] == "rejected"
    rejects = [row for row in env.ops_rows()
               if row["endpoint"] == "POST /v1/approvals/{id}/reject"
               and row["phase"] == "accepted"]
    assert len(rejects) == 2


def test_ac28_interactive_mode_runs_reads_without_confirmation(ctl_api):
    result = run_pty(ctl_api.env.root, ["ctl"], [
        ("afx ctl> ", "whoami"), ("afx ctl> ", "exit")])
    assert result.rc == 0, result.output
    assert "principal: operator" in result.output
    assert "[y/N]" not in result.output


def test_ac28_no_operation_on_non_terminal_is_usage_rc2(ctl_api):
    rc, out, err = run_ctl(ctl_api.env.root, ["--as", "approver"])
    assert (rc, out) == (2, "")
    assert "usage: afx ctl" in err


# ---------------------------------------------------------------- AC-53

def _crash_reset(env, generation: int) -> None:
    result = subprocess.run(
        [sys.executable, str(CRASH_CHILD), str(env.state._path), str(generation),
         "after_marker"],
        capture_output=True, text=True, timeout=30,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    assert result.returncode == 17, result.stderr


@pytest.fixture
def marked(ctl_api):
    env = ctl_api.env
    generation = env.state.update(kill_switch_latched=True).kill_switch_generation
    _crash_reset(env, generation)
    env.state.update(autopilot=True)
    assert env.state.reconcile_marker() is not None
    return env, generation


def test_ac53_reconcile_under_autopilot_shows_marker_then_confirms(marked):
    env, generation = marked
    result = run_pty(env.root, APPROVER + ["killswitch", "reconcile"], [("[y/N]", "y")])
    assert result.rc == 0, result.output
    shown = result.output.split("[y/N]")[0]
    assert f"requested_generation={generation}" in shown
    assert "状態ファイルの値: LATCHED" in shown
    assert "started_at=" in shown and "ラッチ中として確定" in shown
    assert "ラッチ中として確定しました" in result.output
    assert f"killswitch reset {generation}" in result.output
    # 確定はラッチ側だけ。解除は自動で行わない
    assert env.state.reconcile_marker() is None
    assert env.state.load().kill_switch_latched is True


def test_ac53_reconcile_declined_keeps_marker(marked):
    env, _ = marked
    result = run_pty(env.root, APPROVER + ["killswitch", "reconcile"], [("[y/N]", "n")])
    assert result.rc == 1, result.output
    assert env.state.reconcile_marker() is not None


def test_ac53_reconcile_with_piped_stdin_is_not_sent(marked):
    env, _ = marked
    result = run_pty(env.root, APPROVER + ["killswitch", "reconcile"], [], stdin_pipe="y\n")
    assert result.rc == 2, result.output
    assert env.state.reconcile_marker() is not None
    assert env.ops_rows() == []


def test_ac53_reconcile_without_marker_reports_nothing_to_do(ctl_api):
    env = ctl_api.env
    env.state.update(kill_switch_latched=True)
    result = run_pty(env.root, APPROVER + ["killswitch", "reconcile"], [])
    assert result.rc == 0, result.output
    assert "確認が必要な状態はありません" in result.output
    assert env.ops_rows() == []

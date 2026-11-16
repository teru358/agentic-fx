"""`afx ctl killswitch rebase` を実 UDS・実 server・pty で動かす。

この test の server は付け直しの依存 (core lock / broker) を持たないので、適用までは
進まない。端末の扱い (確認なしでは何も送らない) と、server 側の理由の表示を確かめる。"""
from __future__ import annotations

from agentic_fx.ops.contracts import Principal  # noqa: F401

from ._ctl import ctl_api, run_ctl, run_pty  # noqa: F401

APPROVER = ["ctl", "--as", "approver"]


def test_non_terminal_stdin_and_pipe_send_nothing(ctl_api):
    env = ctl_api.env
    generation = env.state.update(kill_switch_latched=True).kill_switch_generation
    rc, out, err = run_ctl(env.root, ["--as", "approver", "killswitch", "rebase",
                                      str(generation)])
    assert (rc, out) == (2, "")
    assert "端末からの確認が要ります" in err
    piped = run_pty(env.root, APPROVER + ["killswitch", "rebase", str(generation)], [],
                    stdin_pipe="rebase %d\n" % generation)
    assert piped.rc == 2, piped.output
    assert env.ops_rows() == []


def test_yes_flag_is_a_usage_error_and_sends_nothing(ctl_api):
    env = ctl_api.env
    rc, out, err = run_ctl(env.root, ["--as", "approver", "killswitch", "rebase", "1",
                                      "--yes"])
    assert (rc, out) == (2, "")
    assert env.ops_rows() == []


def test_operator_cannot_rebase(ctl_api):
    env = ctl_api.env
    rc, out, err = run_ctl(env.root, ["killswitch", "rebase", "1"])
    assert rc == 3 and out == ""
    assert env.ops_rows() == []


def test_unwired_daemon_shows_the_reason_on_a_terminal_and_asks_nothing(ctl_api):
    env = ctl_api.env
    generation = env.state.update(kill_switch_latched=True).kill_switch_generation
    result = run_pty(env.root, APPROVER + ["killswitch", "rebase", str(generation)], [])
    assert result.rc == 1, result.output
    assert "使えません" in result.output
    assert "rebase %d" % generation not in result.output
    assert env.ops_rows() == []

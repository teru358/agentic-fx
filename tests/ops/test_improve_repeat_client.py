"""`afx ctl improve` の option 解釈、受理時 rc=0 の表示、停止の表示、対話シェルの引用。"""
from __future__ import annotations

import io
import re

import pytest

from agentic_fx.core.improve_supervisor import RepeatResult
from agentic_fx.ops import client
from agentic_fx.ops.contracts import Principal

from ._ctl import ctl_api, run_ctl  # noqa: F401
from ._improve_repeat_fake import FakeSupervisor

JOB = "b" * 32
DEADLINE = "2026-10-26T14:30:00+00:00"


def _ok(data):
    return client.Reply(200, data, None)


def _refused(code):
    return client.Reply(409, None, code)


class _StubApi:
    def __init__(self, root, replies, principal=Principal.OPERATOR):
        self.root = root
        self.principal = principal
        self.replies = list(replies)
        self.sent = []

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        self.sent.append((method, path, body, idempotent))
        reply = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        return reply(method, path) if callable(reply) else reply


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _progress(runs=0, mission_id=None, state="running", **extra):
    result = {"mission_id": mission_id, "runs": runs, "max_runs": 3,
              "deadline_at": DEADLINE, "phase": "waiting_slot" if runs == 0 else "running",
              "stop_requested": False}
    result.update(extra)
    return _ok({"id": JOB, "kind": "improve", "state": state, "result": result})


def _run(tmp_path, argv, replies):
    clock = _Clock()
    api = _StubApi(tmp_path, replies)
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out, sleep=clock.sleep,
                           monotonic=clock.monotonic)
    rc = runner.run(client.parse(argv))
    return rc, out.getvalue(), api, clock


def _usage_rc(argv):
    with pytest.raises(client.ClientError) as caught:
        client.parse(argv)
    assert caught.value.rc == 2
    return caught.value


# ---- 文法: 期間 ----

@pytest.mark.parametrize("text,seconds", [
    ("2h", 7200), ("45m", 2700), ("1h30m", 5400), ("90m", 5400), ("24h", 86400),
    ("1m", 60)])
def test_duration_forms_are_accepted(text, seconds):
    assert client.parse(["improve", "-t", text]).args == [seconds, None, None]
    assert client.parse(["improve", f"--duration={text}"]).args == [seconds, None, None]
    assert client.parse(["improve", "--duration", text]).args == [seconds, None, None]


@pytest.mark.parametrize("text", ["30", "1.5h", "1d", "1H", "0m", " 1h", "25h", "24h1m",
                                  "", "1h ", "h", "1m1h", "-1h", "1h-1m", "０５m"])
def test_duration_violations_are_usage(text):
    _usage_rc(["improve", "-t", text])
    _usage_rc(["improve", f"--duration={text}"])


@pytest.mark.parametrize("n", range(1, 25))
def test_max_runs_in_range(n):
    assert client.parse(["improve", "-n", str(n)]).args == [None, n, None]


@pytest.mark.parametrize("text", ["0", "25", "1.5", "abc", "-1", "+1", " 1", "", "１",
                                  "1e1", "0x1"])
def test_max_runs_violations_are_usage(text):
    _usage_rc(["improve", "-n", text])
    _usage_rc(["improve", f"--max-runs={text}"])


def test_the_three_options_combine_in_any_order():
    expected = [5400, 3, "メモ"]
    assert client.parse(["improve", "-t", "1h30m", "-c", "メモ", "-n", "3"]).args == expected
    assert client.parse(["improve", "-n", "3", "-c", "メモ", "-t", "1h30m"]).args == expected
    assert client.parse(["improve", "--comment=メモ", "--max-runs=3",
                         "--duration=1h30m"]).args == expected


@pytest.mark.parametrize("argv", [
    ["improve", "-t", "1h", "-t", "2h"],
    ["improve", "-t", "1h", "--duration", "2h"],
    ["improve", "-n", "1", "-n", "2"],
    ["improve", "-c", "a", "--comment=b"],
    ["improve", "-x"],
    ["improve", "--verbose"],
    ["improve", "-t"],
    ["improve", "-n"],
    ["improve", "-c"],
    ["improve", "--comment"],
    ["improve", "-t1h"],
    ["improve", "-t=1h"],
    ["improve", "foo"],
    ["improve", "-t", "1h", "add", "x"],
    ["improve", "-t", "1h", "add"],
    ["improve", "add"],
    ["improve", "-n", "2", "extra"],
])
def test_duplicates_unknown_missing_values_and_positionals_are_usage(argv):
    _usage_rc(argv)


def test_add_after_other_tokens_is_not_the_legacy_alias_but_before_them_it_is_an_idea():
    op = client.parse(["improve", "add", "x", "-t", "1h"])
    assert (op.name, op.args, op.via_alias) == ("backlog_add", ["x -t 1h"], "improve add")
    op = client.parse(["improve", "add", "-n", "3", "-c", "y"])
    assert (op.name, op.args) == ("backlog_add", ["-n 3 -c y"])
    _usage_rc(["improve", "-t", "1h", "add", "x"])


def test_the_value_of_an_option_is_always_the_next_token():
    assert client.parse(["improve", "-c", "-n"]).args == [None, None, "-n"]
    assert client.parse(["improve", "-c", "-n", "-n", "2"]).args == [None, 2, "-n"]
    assert client.parse(["improve", "-c", "--duration"]).args == [None, None, "--duration"]
    assert client.parse(["improve", "-c", "-n 5"]).args == [None, None, "-n 5"]
    assert client.parse(["improve", "--comment=--max-runs=3"]).args == [
        None, None, "--max-runs=3"]


def test_the_long_form_with_an_equals_keeps_later_equals_in_the_value():
    assert client.parse(["improve", "--comment=a=b"]).args == [None, None, "a=b"]


@pytest.mark.parametrize("comment", ["", "   ", "a\x00b", "a\x07b", "a\u00a0b",
                                     "x" * 1001, "a\u200bb"])
def test_comment_violations_are_usage(comment):
    _usage_rc(["improve", "-c", comment])
    _usage_rc(["improve", f"--comment={comment}"])


def test_comment_is_folded_like_the_daemon_does():
    assert client.parse(["improve", "-c", "  a \t b\n c  "]).args == [None, None, "a b c"]
    assert client.parse(["improve", "-c", "x" * 1000]).args[2] == "x" * 1000


def test_bare_improve_keeps_its_parse():
    op = client.parse(["improve"])
    assert op.name == "improve" and op.args == []


def test_violations_stop_before_the_key_is_read_or_a_socket_is_opened(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(client, "ApiClient", boom)
    err = io.StringIO()
    rc = client.main(["improve", "-t", "30"], root=tmp_path, stdin=io.StringIO(""),
                     stdout=io.StringIO(), stderr=err)
    assert rc == 2 and "afx ctl" in err.getvalue()
    for argv in (["improve", "-n", "99"], ["improve", "-c"], ["improve", "-t", "1h", "-t",
                                                                "1h"]):
        assert client.main(argv, root=tmp_path, stdin=io.StringIO(""), stdout=io.StringIO(),
                           stderr=io.StringIO()) == 2


# ---- 単発 (従来) ----

def test_bare_improve_sends_the_old_request_and_prints_the_old_line(tmp_path):
    rc, out, api, _ = _run(tmp_path, ["improve"], [
        _ok({"job_id": JOB, "state": "running"}),
        _ok({"id": JOB, "state": "done", "result": {"mission_id": 41}})])
    assert rc == 0
    assert api.sent[0] == ("POST", "/v1/improve/runs", {}, True)
    assert out.splitlines() == [f"job {JOB} を受理しました", "improve mission #41 を起動しました"]


def test_comment_only_waits_to_the_end_like_before(tmp_path):
    rc, out, api, _ = _run(tmp_path, ["improve", "-c", "  慎重に  "], [
        _ok({"job_id": JOB, "state": "running"}),
        _ok({"id": JOB, "state": "running", "result": {"mission_id": 41}}),
        _ok({"id": JOB, "state": "done", "result": {"mission_id": 41}})])
    assert rc == 0
    assert api.sent[0][2] == {"comment": "慎重に"}
    assert out.splitlines()[-1] == "improve mission #41 を起動しました"
    assert "慎重に" not in out


def test_comment_only_over_the_poll_limit_is_still_rc1(tmp_path):
    clock = _Clock()
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "running"}),
                              _ok({"id": JOB, "state": "running", "result": None})])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO(),
                           sleep=clock.sleep, monotonic=clock.monotonic)
    assert runner.run(client.parse(["improve", "-c", "x"])) == 1


# ---- 繰り返し: 受理で rc=0 ----

def test_repeat_accepted_prints_the_first_launch_and_returns_zero(tmp_path):
    rc, out, api, clock = _run(tmp_path, ["improve", "-t", "1h30m", "-n", "3", "-c", "秘密"], [
        _ok({"job_id": JOB, "state": "running"}),
        _progress(0),
        _progress(0),
        _progress(1, 41)])
    assert rc == 0
    assert api.sent[0] == ("POST", "/v1/improve/runs",
                           {"duration_sec": 5400, "max_runs": 3, "comment": "秘密"}, True)
    assert [m for m, *_ in api.sent].count("POST") == 1
    assert out.splitlines() == [
        f"job {JOB} を受理しました",
        "improve を繰り返します: 期限 2026-10-26 14:30 UTC (1h30m)、最大 3 回、一時指示あり",
        "1 回目 mission #41 を起動しました",
        f"各 mission の開始・終了は通知に出ます。止める: afx ctl jobs cancel {JOB} "
        "(実行中の mission は完走し、次を始めません)",
        "daemon を再起動すると繰り返しは中断します。"]
    assert "秘密" not in out
    assert clock.sleeps == [0.25, 0.25]


def test_max_runs_only_shows_the_24h_default_span_and_no_comment_note(tmp_path):
    rc, out, api, _ = _run(tmp_path, ["improve", "-n", "3"], [
        _ok({"job_id": JOB, "state": "running"}), _progress(1, 7)])
    assert rc == 0
    assert api.sent[0][2] == {"max_runs": 3}
    assert "(24h)、最大 3 回" in out and "一時指示" not in out


def test_duration_only_sends_no_max_runs(tmp_path):
    rc, out, api, _ = _run(tmp_path, ["improve", "-t", "2h"], [
        _ok({"job_id": JOB, "state": "running"}), _progress(1, 7)])
    assert api.sent[0][2] == {"duration_sec": 7200}
    assert "(2h)、最大 3 回" in out


def test_a_slot_wait_over_45_seconds_is_still_rc0_with_one_post(tmp_path):
    rc, out, api, clock = _run(tmp_path, ["improve", "-n", "2"], [
        _ok({"job_id": JOB, "state": "running"}), _progress(0)])
    assert rc == 0
    assert 45.0 <= clock.now < 45.5
    assert [m for m, *_ in api.sent].count("POST") == 1
    assert "週次の slot が空くのを待っています。1 回目は空き次第始まります" in out
    assert "起動しました" not in out
    assert f"afx ctl jobs cancel {JOB}" in out


def test_state_running_without_a_mission_id_is_not_a_launch(tmp_path):
    rc, out, api, clock = _run(tmp_path, ["improve", "-n", "2"], [
        _ok({"job_id": JOB, "state": "running"}),
        _progress(1, None)])
    assert rc == 0
    assert "起動しました" not in out and "slot が空くのを待って" in out


def test_a_launch_needs_both_runs_and_mission_id(tmp_path):
    _, out, _, _ = _run(tmp_path, ["improve", "-n", "2"], [
        _ok({"job_id": JOB, "state": "running"}), _progress(0, 41)])
    assert "起動しました" not in out


@pytest.mark.parametrize("job,needle", [
    ({"state": "failed", "result_code": "autopilot_restricted", "result": None},
     "autopilot 中はこの操作を受け付けません"),
    ({"state": "failed", "result_code": "internal",
      "result": {"runs": 0, "mission_id": None, "max_runs": 2, "deadline_at": DEADLINE}},
     "daemon の内部エラーです"),
    ({"state": "done", "result": {"runs": 0, "mission_id": None, "max_runs": 2,
                                  "deadline_at": DEADLINE, "stop_reason": "autopilot"}},
     "autopilot 中のため終了しました"),
    ({"state": "done", "result": {"runs": 0, "mission_id": None, "max_runs": 2,
                                  "deadline_at": DEADLINE, "stop_reason": "shutdown"}},
     "daemon の停止により終了しました"),
    ({"state": "done", "result": {"runs": 0, "mission_id": None, "max_runs": 2,
                                  "deadline_at": DEADLINE, "stop_reason": "stopped"}},
     "停止の要求により終了しました"),
    ({"state": "shutdown", "result_code": "shutdown", "result": None},
     "daemon の停止により終了しました"),
])
def test_termination_after_acceptance_is_rc0_with_the_reason_and_job_id(
        tmp_path, job, needle):
    reply = _ok({"id": JOB, "kind": "improve", **job})
    rc, out, api, _ = _run(tmp_path, ["improve", "-n", "2"], [
        _ok({"job_id": JOB, "state": "running"}), reply])
    assert rc == 0
    assert needle in out
    assert f"job {JOB}:" in out
    assert [m for m, *_ in api.sent].count("POST") == 1


def test_termination_after_runs_names_the_count_and_shows_the_launch(tmp_path):
    reply = _ok({"id": JOB, "kind": "improve", "state": "done", "result": {
        "runs": 3, "mission_id": 9, "max_runs": 3, "deadline_at": DEADLINE,
        "stop_reason": "max_runs", "phase": "finished"}})
    rc, out, _, _ = _run(tmp_path, ["improve", "-n", "3"], [
        _ok({"job_id": JOB, "state": "running"}), reply])
    assert rc == 0
    assert "1 回目 mission #9 を起動しました" in out
    assert "最大回数に達したため終了しました (実施 3 回)" in out


@pytest.mark.parametrize("code", ["improve_running", "autopilot_restricted", "invalid_argument"])
def test_a_synchronous_refusal_is_rc1_and_creates_nothing(tmp_path, code):
    api = _StubApi(tmp_path, [_refused(code)])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.parse(["improve", "-n", "2"]))
    assert caught.value.rc == 1
    assert len(api.sent) == 1


def test_ctrl_c_while_waiting_leaves_the_job_and_reports_it(tmp_path):
    def interrupt(_seconds):
        raise KeyboardInterrupt

    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "running"}), _progress(0)])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out, sleep=interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run(client.parse(["improve", "-n", "2"]))
    assert "受理済みで、この client を離れても続きます" in out.getvalue()
    assert [p for _, p, _, _ in api.sent if "cancel" in p] == []


def test_a_lost_poll_after_acceptance_names_the_job(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "running"}),
                              _refused("internal")])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    with pytest.raises(client.ClientError):
        runner.run(client.parse(["improve", "-n", "2"]))
    assert JOB in out.getvalue()


# ---- jobs cancel / get の表示 ----

class _Yes:
    def yes(self, prompt):
        return True


def test_cancel_of_a_running_repeat_says_the_stop_was_accepted(tmp_path):
    api = _StubApi(tmp_path, [_ok({"id": JOB, "state": "running", "stop_requested": True})])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    assert runner._op_job_cancel(JOB, terminal=_Yes()) == 0
    assert "停止を受け付けました。実行中の mission の終了後に止まります" in out.getvalue()


def test_cancel_of_a_queued_job_keeps_the_old_line(tmp_path):
    api = _StubApi(tmp_path, [_ok({"id": JOB, "state": "cancelled"})])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    runner._op_job_cancel(JOB, terminal=_Yes())
    assert out.getvalue().strip() == f"job {JOB}: cancelled"


def test_jobs_get_shows_progress_without_a_comment(tmp_path):
    api = _StubApi(tmp_path, [_progress(2, 41, stop_requested=True)])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    runner.run(client.parse(["jobs", "get", JOB]))
    line = out.getvalue()
    for cell in ("runs=2", "max_runs=3", "mission_id=41", "phase=running",
                 "stop_requested=True", f"deadline_at={DEADLINE}"):
        assert cell in line


# ---- 対話シェル ----

def _shell(monkeypatch, text):
    seen = []

    class FakeApi:
        def __init__(self, *a, **k):
            self.principal = Principal.OPERATOR

    monkeypatch.setattr(client, "ApiClient", FakeApi)
    monkeypatch.setattr(client.Runner, "run", lambda self, op: seen.append(op) or 0)
    out, err = io.StringIO(), io.StringIO()
    rc = client._interactive(None, Principal.OPERATOR, uid=None, stdin=io.StringIO(text),
                             stdout=out, stderr=err, sleep=lambda s: None)
    return rc, seen, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("line,args", [
    ("improve -c 'a b' -n 2", [None, 2, "a b"]),
    ('improve -c "a b" -n 2', [None, 2, "a b"]),
    ("improve -c a\\ b -t 1h", [3600, None, "a b"]),
    ("improve -c \"it's\"", [None, None, "it's"]),
    ("improve -c 'say \\\"x\\\"'", [None, None, 'say \\"x\\"']),
    ('improve -c "say \\"x\\""', [None, None, 'say "x"']),
    ("improve -c '-n 5' -n 2", [None, 2, "-n 5"]),
    ("improve -c -n", [None, None, "-n"]),
])
def test_the_improve_line_uses_shell_quoting(monkeypatch, line, args):
    rc, seen, out, err = _shell(monkeypatch, line + "\n")
    assert rc == 0 and err == ""
    assert [op.args for op in seen] == [args]


def test_an_empty_comment_is_usage_and_the_shell_continues(monkeypatch):
    rc, seen, out, err = _shell(monkeypatch, "improve -c ''\nwhoami\n")
    assert rc == 0
    assert "usage" in err
    assert [op.name for op in seen] == ["whoami"]


def test_a_quote_mismatch_reports_and_continues(monkeypatch):
    rc, seen, out, err = _shell(monkeypatch, "improve -c 'oops\nwhoami\n")
    assert rc == 0
    assert "引用符が閉じていません" in err
    assert [op.name for op in seen] == ["whoami"]


def test_other_commands_still_split_on_whitespace_only(monkeypatch):
    rc, seen, out, err = _shell(
        monkeypatch, "ask what's up \\ now\nreject 3 it's \\bad\nbacklog add it's fine\n")
    assert rc == 0 and err == ""
    assert [op.args for op in seen] == [
        ["what's up \\ now"], [3, "it's \\bad"], ["it's fine"]]


def test_legacy_improve_add_keeps_whitespace_splitting(monkeypatch):
    rc, seen, out, err = _shell(monkeypatch, "improve add it's -n 3\n")
    assert rc == 0 and err == ""
    assert [(op.name, op.args) for op in seen] == [("backlog_add", ["it's -n 3"])]


# ---- 実 server を通す ----

def test_a_repeat_over_the_real_server_returns_zero_with_the_job_running(ops_env):
    from ._api import make_api
    from agentic_fx.store.instance_lock import acquire_instance_lock
    import threading

    release = threading.Event()

    def repeat(*, on_progress, cancel_event, **kw):
        on_progress({"phase": "running", "runs": 1, "mission_id": 41})
        release.wait(10)
        return RepeatResult("stopped", 1, 41)

    sup = FakeSupervisor(repeat)
    api_env = make_api(ops_env, service_pid=0, service_kwargs={"improve_supervisor": sup})
    lock = acquire_instance_lock(ops_env.root / "data")
    try:
        rc, out, err = run_ctl(ops_env.root, ["improve", "-n", "2", "-c", "メモ"])
        assert rc == 0, err
        assert "1 回目 mission #41 を起動しました" in out
        job_id = re.search(r"job ([0-9a-f]{32}) を受理しました", out).group(1)
        assert api_env.service.get_job(Principal.OPERATOR, job_id)["state"] == "running"
        assert sup.repeat_calls[0]["max_runs"] == 2
        assert sup.repeat_calls[0]["comment_holder"] is not None
        # 走行中は 2 本目を受けない。
        rc, _, err = run_ctl(ops_env.root, ["improve"])
        assert rc == 1 and "improve が既に実行中" in err
    finally:
        release.set()
        api_env.server.stop(5.0)
        lock.close()

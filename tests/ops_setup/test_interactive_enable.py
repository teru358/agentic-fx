from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field

import pytest

from agentic_fx.ops.keys import KeySetError
from agentic_fx.ops_setup.feature_config import collect, write_inputs
from agentic_fx.ops_setup.interactive import ConsoleIO, interactive_enable
from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
from agentic_fx.ops_setup.stages import StageResult
from agentic_fx.ops_setup.systemd import SystemdError

TOKEN = "tok-SECRET-abc123.XYZ"
WEBHOOK = "https://hooks.example.com/api/webhooks/987654/SECRET-PATH-xyz"
G, C, U = 10**16 + 1, 10**16 + 2, 10**16 + 3

BOT = {"guild_id": G, "channel_id": C, "allowlist_user_ids": [U], "mention_user_ids": [U], "token": TOKEN}
VALUES = {"bot": BOT, "web": {"port": 8788}, "notify": {"webhook_url": WEBHOOK}}


@dataclass
class FakeIO:
    answers: list = field(default_factory=list)
    tty: bool = True
    out: list = field(default_factory=list)
    err: list = field(default_factory=list)
    asked: list = field(default_factory=list)
    secrets: list = field(default_factory=list)
    raise_after: object = None  # (count, exception) once that many answers were consumed

    def _next(self, label):
        if self.raise_after is not None and len(self.asked) + len(self.secrets) >= self.raise_after[0]:
            raise self.raise_after[1]
        return self.answers.pop(0)

    def isatty(self): return self.tty

    def ask(self, label, default=None):
        value = self._next(label)
        self.asked.append(label)
        return value or default

    def ask_secret(self, label):
        value = self._next(label)
        self.secrets.append(label)
        return value

    def stdout(self, text): self.out.append(text)
    def stderr(self, text): self.err.append(text)


class Setup:
    def __init__(self, errors=(), state="inactive"):
        self.errors = list(errors)
        self.enabled = []
        self.systemd = type("Systemd", (), {"show": lambda *_: {"ActiveState": state}})()
        self.paths = type("Paths", (), {"afx_unit": "afx.service"})()

    def enable(self, feature):
        self.enabled.append(feature)
        if self.errors:
            raise self.errors.pop(0)
        return StageResult(feature, "enable", "enabled")


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "agentic-fx" / "ops"


def run(root, feature, io, *, probe=lambda _f: (0, ""), setup=None, keys_init=lambda: 0):
    setup = setup or Setup()
    result = interactive_enable(root, feature, io=io, probe_runner=probe, setup_factory=lambda _r: setup, keys_init=keys_init)
    return result.rc, setup


def test_held_lease_runs_enable_without_reacquiring_setup_lock(tmp_path, xdg, monkeypatch):
    write_inputs(tmp_path, "web", {"port": 8788})
    lease = acquire_setup_lease(tmp_path)
    setup = Setup()
    try:
        monkeypatch.setattr("agentic_fx.ops_setup.interactive.acquire_setup_lock",
                            lambda _root: (_ for _ in ()).throw(AssertionError("lock reacquired")))
        result = interactive_enable(tmp_path, "web", io=FakeIO(), lease=lease,
                                    probe_runner=lambda _feature: (0, ""),
                                    setup_factory=lambda _root: setup)
    finally:
        lease.close()
    assert result.rc == 0
    assert result.stage == "enabled"
    assert result.reason_code == "ok"
    assert setup.enabled == ["web"]


def test_wrong_or_closed_lease_is_rejected_before_io_or_enable(tmp_path, xdg):
    (tmp_path / "other").mkdir()
    wrong = acquire_setup_lease(tmp_path / "other")
    closed = acquire_setup_lease(tmp_path)
    closed.close()
    try:
        for lease in (wrong, closed):
            io = FakeIO()
            setup = Setup()
            result = interactive_enable(tmp_path, "web", io=io, lease=lease,
                                        setup_factory=lambda _root: setup)
            assert (result.rc, result.stage, result.reason_code) == (1, "precheck", "setup_lease_invalid")
            assert io.out == [] and io.err == [] and io.asked == [] and setup.enabled == []
    finally:
        wrong.close()


def test_missing_configuration_without_ops_io_returns_structured_reason(tmp_path, xdg):
    class NotATerminal:
        def isatty(self): return False

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "stdin", NotATerminal())
    try:
        result = interactive_enable(tmp_path, "web")
    finally:
        monkeypatch.undo()
    assert (result.rc, result.stage, result.reason_code) == (2, "config", "setup_input_required")


def test_lease_without_io_collects_missing_values_from_terminal(tmp_path, xdg, monkeypatch):
    class TerminalInput:
        def __init__(self): self.lines = iter(["9000\n"])
        def isatty(self): return True
        def readline(self): return next(self.lines)

    class TerminalOutput:
        def isatty(self): return True
        def write(self, _text): return 0
        def flush(self): return None

    monkeypatch.setattr(sys, "stdin", TerminalInput())
    monkeypatch.setattr(sys, "stdout", TerminalOutput())
    lease = acquire_setup_lease(tmp_path)
    setup = Setup()
    try:
        result = interactive_enable(tmp_path, "web", lease=lease,
                                    probe_runner=lambda _feature: (0, ""),
                                    setup_factory=lambda _root: setup)
    finally:
        lease.close()
    assert result.rc == 0
    assert setup.enabled == ["web"]
    assert collect(tmp_path, "web").values == {"port": 9000}


def test_lease_without_io_on_non_terminal_does_not_ask(tmp_path, xdg, monkeypatch):
    calls = []

    class NotATerminal:
        def isatty(self): return False
        def readline(self):
            calls.append("read")
            return "9000\n"

    monkeypatch.setattr(sys, "stdin", NotATerminal())
    lease = acquire_setup_lease(tmp_path)
    try:
        result = interactive_enable(tmp_path, "web", lease=lease)
    finally:
        lease.close()
    assert (result.rc, result.stage, result.reason_code) == (2, "config", "setup_input_required")
    assert calls == []


def test_probe_reason_is_structured_not_derived_from_output_text(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    first = interactive_enable(tmp_path, "web", io=FakeIO(tty=False),
                               probe_runner=lambda _feature: (69, "config_invalid"))
    second = interactive_enable(tmp_path, "web", io=FakeIO(tty=False),
                                probe_runner=lambda _feature: (69, "changed diagnostic text"))
    assert (first.rc, first.stage, first.reason_code) == (1, "probe", "config_invalid")
    assert (second.rc, second.stage, second.reason_code) == (1, "probe", "feature_probe_unreadable")


def test_enable_reason_code_does_not_depend_on_error_detail(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    results = []
    for detail in ("first diagnostic", "different diagnostic"):
        result = interactive_enable(tmp_path, "web", io=FakeIO(),
                                    probe_runner=lambda _feature: (0, ""),
                                    setup_factory=lambda _root, d=detail: Setup([SystemdError("start", d)]))
        results.append((result.rc, result.stage, result.reason_code))
    assert results == [(1, "enable", "start"), (1, "enable", "start")]


def test_enable_reason_code_rejects_freeform_exception_reason(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})

    class FreeformFailure(RuntimeError):
        reason = "diagnostic containing a URL https://example.test/secret"

    result = interactive_enable(tmp_path, "web", io=FakeIO(),
                                probe_runner=lambda _feature: (0, ""),
                                setup_factory=lambda _root: Setup([FreeformFailure()]))
    assert (result.rc, result.stage, result.reason_code) == (1, "enable", "unknown")


def test_lease_remains_flocked_during_enable(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    lease = acquire_setup_lease(tmp_path)
    lock_path = tmp_path / "data" / "ops" / "setup.lock"

    class CheckingSetup(Setup):
        def enable(self, feature):
            child = subprocess.run(
                [sys.executable, "-c",
                 "import errno,fcntl,sys; f=open(sys.argv[1], 'a+b');\n"
                 "try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                 "except BlockingIOError as error: raise SystemExit(7 if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK) else 8)\n"
                 "raise SystemExit(9)", str(lock_path)],
                capture_output=True, text=True, timeout=30,
            )
            assert child.returncode == 7, child.stderr
            return super().enable(feature)

    setup = CheckingSetup()
    try:
        result = interactive_enable(tmp_path, "web", io=FakeIO(), lease=lease,
                                    probe_runner=lambda _feature: (0, ""),
                                    setup_factory=lambda _root: setup)
    finally:
        lease.close()
    assert result.rc == 0 and setup.enabled == ["web"]


def test_non_tty_missing_inputs_stops_without_prompt(tmp_path, xdg):
    io = FakeIO(tty=False)
    rc, setup = run(tmp_path, "web", io)
    assert rc == 2
    assert io.err == ["afx ops: setup_input_required"]
    assert io.asked == [] and setup.enabled == []
    assert not xdg.exists()


def test_console_io_requires_tty_stdin_and_stdout(monkeypatch):
    class Stream:
        def __init__(self, tty): self.tty = tty
        def isatty(self): return self.tty
    monkeypatch.setattr(sys, "stdin", Stream(True))
    monkeypatch.setattr(sys, "stdout", Stream(False))
    assert not ConsoleIO().isatty()
    monkeypatch.setattr(sys, "stdout", Stream(True))
    assert ConsoleIO().isatty()
    monkeypatch.setattr(sys, "stdin", Stream(False))
    assert not ConsoleIO().isatty()


def test_probe_failure_reasks_then_enables_once(tmp_path, xdg):
    io = FakeIO(answers=["8788", "8789"])
    seen = []

    def probe(feature):
        seen.append(feature)
        return (73, "probe_bind_failed") if len(seen) == 1 else (0, "")

    rc, setup = run(tmp_path, "web", io, probe=probe)
    assert rc == 0
    assert seen == ["web", "web"]
    assert setup.enabled == ["web"]
    assert io.err == ["afx-ops: probe_bind_failed"]
    assert collect(tmp_path, "web").values == {"port": 8789}


def test_probe_failure_three_times_gives_up_without_enable(tmp_path, xdg):
    io = FakeIO(answers=["8788", "8788", "8788"])
    rc, setup = run(tmp_path, "web", io, probe=lambda _f: (71, "config_invalid"))
    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx-ops: config_invalid"] * 3


def test_probe_failure_non_tty_with_existing_ends_after_one_probe(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    calls = []
    io = FakeIO(tty=False)
    rc, setup = run(tmp_path, "web", io, probe=lambda f: calls.append(f) or (73, "probe_x"))
    assert rc == 1 and calls == ["web"] and setup.enabled == [] and io.asked == []


def test_probe_non_retry_status_stops_without_reasking(tmp_path, xdg):
    io = FakeIO(answers=["8788", "8789"])
    rc, setup = run(tmp_path, "web", io, probe=lambda _f: (69, "config_invalid"))
    assert rc == 1 and setup.enabled == []
    assert io.asked == ["port"]
    assert io.err == ["afx-ops: config_invalid"]


def test_probe_reason_is_never_untrusted_output(tmp_path, xdg):
    io = FakeIO(answers=["8788"])
    rc, setup = run(tmp_path, "web", io, probe=lambda _f: (69, "https://example/SECRET\nconfig_invalid"))
    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx-ops: feature_probe_unreadable"]


def test_unsafe_existing_config_fails_closed_without_rewriting(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    config = xdg / "web" / "config" / "ops.toml"
    config.chmod(0o666)
    before = config.read_bytes()
    io = FakeIO(answers=["9999"])
    rc, setup = run(tmp_path, "web", io)
    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: feature_config_unsafe:config_file"]
    assert config.read_bytes() == before


def test_read_only_secret_fails_closed_without_prompt_or_enable(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    secret = xdg / "bot" / "secret" / "discord.token"
    secret.chmod(0o400)
    before = secret.read_bytes()
    io = FakeIO(answers=["replacement"])

    rc, setup = run(tmp_path, "bot", io)

    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: feature_config_unsafe:secret_file"]
    assert io.asked == [] and io.secrets == []
    assert secret.read_bytes() == before


def test_unknown_config_key_fails_closed_without_rewriting(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    config = xdg / "web" / "config" / "ops.toml"
    config.write_text(config.read_text() + 'manual_value = "keep"\n')
    before = config.read_bytes()
    io = FakeIO()
    rc, setup = run(tmp_path, "web", io)
    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: feature_config_unsafe:config_file"]
    assert config.read_bytes() == before


def test_broken_toml_fails_closed_without_rewriting(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    config = xdg / "web" / "config" / "ops.toml"
    config.write_text("not = [toml")
    before = config.read_bytes()
    io = FakeIO(answers=["9999"])
    rc, setup = run(tmp_path, "web", io)
    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: feature_config_unsafe:config_file"]
    assert config.read_bytes() == before


def test_group_writable_feature_parents_are_complete_without_prompting(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    xdg.parent.chmod(0o775)
    xdg.chmod(0o775)
    io = FakeIO()

    rc, setup = run(tmp_path, "bot", io)

    assert rc == 0 and setup.enabled == ["bot"]
    assert io.asked == [] and io.secrets == []


def test_other_writable_feature_parent_has_fixed_unsafe_reason(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    xdg.chmod(0o777)
    io = FakeIO()

    rc, setup = run(tmp_path, "web", io)

    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: feature_config_unsafe:parent_dir"]
    assert io.asked == [] and io.secrets == []


def test_feature_symlink_has_fixed_unsafe_reason(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    feature = xdg / "web"
    target = tmp_path / "target"
    feature.rename(target)
    feature.symlink_to(target, target_is_directory=True)
    io = FakeIO()

    rc, setup = run(tmp_path, "web", io)

    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: feature_config_unsafe:symlink"]
    assert io.asked == [] and io.secrets == []


def test_bot_retry_keeps_mentions_equal_to_reentered_allowlist(tmp_path, xdg):
    newer = U + 1
    io = FakeIO(answers=[TOKEN, str(G), str(C), str(U), TOKEN, str(G), str(C), str(newer)])
    results = iter([(73, "config_invalid"), (0, "")])
    rc, _ = run(tmp_path, "bot", io, probe=lambda _f: next(results))
    assert rc == 0
    assert collect(tmp_path, "bot").values["mention_user_ids"] == [newer]


def test_not_initialized_inactive_initializes_then_retries_enable(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    setup = Setup([KeySetError("not_initialized")])
    initialized = []
    rc, _ = run(tmp_path, "web", FakeIO(), setup=setup, keys_init=lambda: initialized.append(True) or 0)
    assert rc == 0
    assert initialized == [True]
    assert setup.enabled == ["web", "web"]


@pytest.mark.parametrize("state", ["active", "activating", "reloading"])
def test_not_initialized_with_running_afx_stops_with_fixed_reason(tmp_path, xdg, state):
    write_inputs(tmp_path, "web", {"port": 8788})
    setup = Setup([KeySetError("not_initialized")], state=state)
    initialized = []
    io = FakeIO()
    rc, _ = run(tmp_path, "web", io, setup=setup, keys_init=lambda: initialized.append(True) or 0)
    assert rc == 1
    assert initialized == []
    assert setup.enabled == ["web"]
    assert len(io.err) == 1 and io.err[0].startswith("afx ops: afx_running_keys_not_initialized")


def test_not_initialized_keys_init_failure_does_not_retry_enable(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    setup = Setup([KeySetError("not_initialized")])
    rc, _ = run(tmp_path, "web", FakeIO(), setup=setup, keys_init=lambda: 1)
    assert rc == 1 and setup.enabled == ["web"]


def test_other_key_errors_are_reported_not_initialized_over(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    setup = Setup([KeySetError("key_dir_invalid")])
    initialized = []
    io = FakeIO()
    rc, _ = run(tmp_path, "web", io, setup=setup, keys_init=lambda: initialized.append(1) or 0)
    assert rc == 1 and initialized == []
    assert io.err == ["afx ops: key_dir_invalid"]


# (a) minimal question sets
@pytest.mark.parametrize("feature,answers,asked,secrets", [
    ("bot", [TOKEN, str(G), str(C), str(U)], ["guild ID", "channel ID", "allowlist user IDs (comma separated)"], ["Discord token"]),
    ("web", ["9000"], ["port"], []),
    ("notify", [WEBHOOK], [], ["webhook URL"]),
])
def test_questions_are_minimal_per_feature(tmp_path, xdg, feature, answers, asked, secrets):
    io = FakeIO(answers=list(answers))
    rc, setup = run(tmp_path, feature, io)
    assert rc == 0 and setup.enabled == [feature]
    assert io.asked == asked and io.secrets == secrets
    assert io.answers == []
    assert collect(tmp_path, feature).complete


def test_bot_defaults_mention_users_to_allowlist(tmp_path, xdg):
    io = FakeIO(answers=[TOKEN, str(G), str(C), f"{U}, {U + 1}"])
    run(tmp_path, "bot", io)
    text = (xdg / "bot" / "config" / "ops.toml").read_text()
    assert f"allowlist_user_ids = [{U}, {U + 1}]" in text
    assert f"mention_user_ids = [{U}, {U + 1}]" in text


def test_bot_with_only_token_missing_asks_only_token(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    (xdg / "bot" / "secret" / "discord.token").unlink()
    io = FakeIO(answers=["new-token-value"])
    rc, setup = run(tmp_path, "bot", io)
    assert rc == 0 and io.asked == [] and io.secrets == ["Discord token"]
    assert (xdg / "bot" / "secret" / "discord.token").read_text() == "new-token-value"


def test_bot_with_only_config_missing_keeps_secret_untouched(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    secret = xdg / "bot" / "secret" / "discord.token"
    before = (secret.read_bytes(), secret.stat().st_mtime_ns)
    (xdg / "bot" / "config" / "ops.toml").unlink()
    io = FakeIO(answers=[str(G), str(C), str(U)])
    rc, _ = run(tmp_path, "bot", io)
    assert rc == 0 and io.secrets == []
    assert (secret.read_bytes(), secret.stat().st_mtime_ns) == before


# (b) secrets never leak


@pytest.mark.parametrize("feature,answers,needles", [
    ("bot", [TOKEN, str(G), str(C), str(U)], [TOKEN]),
    ("notify", [WEBHOOK], ["SECRET-PATH-xyz", "987654"]),
])
def test_secrets_never_appear_in_output_or_non_secret_files(tmp_path, xdg, feature, answers, needles):
    io = FakeIO(answers=list(answers))
    probes = iter([(73, "probe_failed"), (0, "")])
    # first probe fails, forcing the retry path, which must not echo values either
    io.answers += list(answers)
    rc, _ = run(tmp_path, feature, io, probe=lambda _f: next(probes))
    assert rc == 0
    secret_dir = xdg / feature / "secret"
    haystack = "\n".join(io.out + io.err + io.asked + io.secrets)
    for path in tmp_path.rglob("*"):
        if path.is_file() and secret_dir not in path.parents:
            haystack += "\n" + path.read_text("utf-8", errors="replace")
    for needle in needles:
        assert needle not in haystack
    # the secret itself is on disk only under the secret directory
    assert any(needles[0] in p.read_text() for p in secret_dir.iterdir())


def test_invalid_input_message_does_not_echo_the_value(tmp_path, xdg):
    bad = "http://hooks.example.com/SECRET-PATH-xyz"
    io = FakeIO(answers=[bad, WEBHOOK])
    rc, setup = run(tmp_path, "notify", io)
    assert rc == 0 and setup.enabled == ["notify"]
    assert io.err == ["afx ops: setup_input_invalid"]
    assert "SECRET-PATH" not in "\n".join(io.out + io.err)


# (d) complete existing inputs: zero questions, files untouched
@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_complete_existing_asks_nothing_and_leaves_files_untouched(tmp_path, xdg, feature):
    write_inputs(tmp_path, feature, VALUES[feature])
    base = xdg / feature
    files = sorted(p for p in base.rglob("*") if p.is_file())
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    probes = []
    io = FakeIO(tty=True)
    for _ in range(2):
        rc, setup = run(tmp_path, feature, io, probe=lambda f: probes.append(f) or (0, ""))
        assert rc == 0 and setup.enabled == [feature]
    assert io.asked == [] and io.secrets == [] and io.err == []
    assert probes == [feature, feature]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(p for p in base.rglob("*") if p.is_file())} == before


def test_complete_existing_non_tty_still_probes_and_enables(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    rc, setup = run(tmp_path, "web", FakeIO(tty=False))
    assert rc == 0 and setup.enabled == ["web"]


# (g) setup.lock
def test_setup_lock_held_by_another_process_is_setup_busy(tmp_path, xdg):
    lock = tmp_path / "data" / "ops" / "setup.lock"
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c",
         "import fcntl,sys,time\nf=open(sys.argv[1],'a+b')\nfcntl.flock(f,fcntl.LOCK_EX)\nprint('held',flush=True)\nsys.stdin.readline()",
         str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        io = FakeIO()
        rc, setup = run(tmp_path, "web", io)
        assert rc == 1 and io.err == ["afx ops: setup_busy"]
        assert setup.enabled == [] and io.asked == [] and not xdg.exists()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
    lock.chmod(0o600)
    rc, setup = run(tmp_path, "web", FakeIO(answers=["8788"]))
    assert rc == 0 and setup.enabled == ["web"]


def test_setup_lock_other_writable_directory_is_unsafe(tmp_path, xdg):
    lock_dir = tmp_path / "data" / "ops"
    lock_dir.mkdir(parents=True)
    lock_dir.chmod(0o777)
    io = FakeIO(answers=["8788"])

    rc, setup = run(tmp_path, "web", io)

    assert rc == 1 and setup.enabled == []
    assert io.err == ["afx ops: setup_lock_unsafe"]
    assert io.asked == [] and io.secrets == []
    assert not xdg.exists()


def test_setup_lock_is_released_after_failure(tmp_path, xdg):
    rc, _ = run(tmp_path, "web", FakeIO(answers=["8788"]), probe=lambda _f: 1 / 0)
    assert rc == 1
    rc, setup = run(tmp_path, "web", FakeIO())
    assert rc == 0 and setup.enabled == ["web"]


# (h) modes via fstat
def _mode(path):
    flags = os.O_RDONLY | (os.O_DIRECTORY if path.is_dir() else 0)
    fd = os.open(path, flags)
    try:
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


@pytest.mark.parametrize("feature,secret", [("bot", "discord.token"), ("notify", "webhook.url"), ("web", None)])
def test_interactive_write_modes(tmp_path, xdg, feature, secret):
    answers = {"bot": [TOKEN, str(G), str(C), str(U)], "notify": [WEBHOOK], "web": ["8788"]}[feature]
    run(tmp_path, feature, FakeIO(answers=answers))
    base = xdg / feature
    assert _mode(base) == 0o700
    assert _mode(base / "config") == 0o755
    assert _mode(base / "secret") == 0o700
    assert _mode(base / "config" / "ops.toml") == 0o644
    if secret:
        assert _mode(base / "secret" / secret) == 0o600
    else:
        assert list((base / "secret").iterdir()) == []


# (i) abort
@pytest.mark.parametrize("exc", [EOFError(), KeyboardInterrupt()])
@pytest.mark.parametrize("feature,after", [("bot", 0), ("bot", 1), ("bot", 3), ("web", 0), ("notify", 0)])
def test_abort_during_input_leaves_no_files(tmp_path, xdg, exc, feature, after):
    answers = {"bot": [TOKEN, str(G), str(C), str(U)], "notify": [WEBHOOK], "web": ["8788"]}[feature]
    io = FakeIO(answers=list(answers), raise_after=(after, exc))
    rc, setup = run(tmp_path, feature, io)
    assert rc == 1
    assert io.err == ["afx ops: setup_aborted"]
    assert setup.enabled == []
    assert not [p for p in xdg.rglob("*") if p.is_file()]


def test_abort_on_retry_leaves_only_complete_files(tmp_path, xdg):
    io = FakeIO(answers=["8788"], raise_after=(1, EOFError()))
    rc, setup = run(tmp_path, "web", io, probe=lambda _f: (73, "probe_bind_failed"))
    assert rc == 1 and io.err == ["afx-ops: probe_bind_failed", "afx ops: setup_aborted"]
    files = [p for p in xdg.rglob("*") if p.is_file()]
    assert [p.name for p in files] == ["ops.toml"]
    assert collect(tmp_path, "web").values == {"port": 8788}
    assert setup.enabled == []

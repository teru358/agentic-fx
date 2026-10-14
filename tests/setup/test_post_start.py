from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agentic_fx.ops_setup.interactive import EnableResult
from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
from agentic_fx.setup.post_start import (
    enable_and_start,
    handoff_and_enable,
    isolation_record_values,
    ops_record_values,
    precheck_ops,
    read_admission_line,
)
from agentic_fx.store.instance_lock import acquire_instance_lock
from agentic_fx.store.setup_marker import setup_marker_present
from agentic_fx.store.setup_record import _valid


@dataclass
class Completed:
    returncode: int = 0
    stdout: str = ""


class Run:
    def __init__(self, replies=()):
        self.calls: list[list[str]] = []
        self.replies = list(replies)

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        reply = self.replies.pop(0) if self.replies else Completed()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def test_precheck_ops_returns_later_guidance_when_resolution_fails(tmp_path, monkeypatch):
    import agentic_fx.setup.post_start as module

    monkeypatch.setattr(module, "resolve_ops_exec",
                        lambda _paths: (_ for _ in ()).throw(module.OpsExecNotFound("missing")))
    result = precheck_ops(tmp_path)
    assert result.available is False
    assert result.reason_code == "ops_exec_not_found"
    assert "PATH" in result.guidance


def test_handoff_releases_instance_lock_enables_every_feature_then_removes_marker(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()
    seen: list[tuple[str, str | bool]] = []

    def enable(root, feature, *, lease):
        assert root == tmp_path
        assert lease.active
        try:
            acquire_instance_lock(tmp_path / "data").close()
            lock_is_free = True
        except Exception:
            lock_is_free = False
        seen.append((feature, lock_is_free))
        return EnableResult(1 if feature == "web" else 0, "enable", "ops_not_ready" if feature == "web" else "ok")

    try:
        outcomes = handoff_and_enable(tmp_path, lease, ["bot", "web"], enable=enable,
                                      now=lambda: "2026-10-13T00:00:00Z",
                                      precheck=lambda _root: type("Check", (), {"available": True})())
    finally:
        lease.close()
    assert seen == [("bot", True), ("web", True)]
    assert [(item.feature, item.result, item.stage, item.reason_code) for item in outcomes] == [
        ("bot", "enabled", "enable", "ok"), ("web", "failed", "enable", "ops_not_ready")]
    assert not setup_marker_present(tmp_path)


def test_handoff_keeps_marker_when_enable_raises(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            handoff_and_enable(tmp_path, lease, ["web"],
                               enable=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
                               now=lambda: "2026-10-13T00:00:00Z",
                               precheck=lambda _root: type("Check", (), {"available": True})())
        assert setup_marker_present(tmp_path)
    finally:
        lease.close()


def test_handoff_orders_marker_lock_release_enable_and_removal(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()
    events: list[str] = []

    def hook(name):
        if name in {"after_handoff_marker", "before_handoff_instance_lock_release",
                    "after_handoff_instance_lock_release", "before_setup_marker_remove",
                    "after_setup_marker_unlink"}:
            events.append(name)

    try:
        handoff_and_enable(
            tmp_path, lease, ["web"], hook=hook,
            enable=lambda *_args, **_kwargs: (events.append("enable") or EnableResult(0, "enable", "ok")),
            now=lambda: "2026-10-13T00:00:00Z",
            precheck=lambda _root: type("Check", (), {"available": True})(),
        )
    finally:
        lease.close()
    assert events == ["after_handoff_marker", "before_handoff_instance_lock_release",
                      "after_handoff_instance_lock_release", "enable", "before_setup_marker_remove",
                      "after_setup_marker_unlink"]


@pytest.mark.parametrize(("replies", "reason"), [
    ([Completed(1)], "unit_enable_failed"),
    ([Completed(), Completed(), Completed(stdout="-- cursor: before\n"), Completed(1)], "unit_start_failed"),
])
def test_start_failures_have_fixed_reasons(tmp_path, replies, reason):
    result = enable_and_start(tmp_path, run=Run(replies), confirm=lambda _step: True,
                              now=lambda: "2026-10-13T00:00:00Z")
    assert result.reason_code == reason


def test_start_records_active_api_and_ops_independently(tmp_path):
    run = Run([Completed(), Completed(), Completed(stdout="-- cursor: before\n"), Completed(), Completed(stdout="new\n"), Completed(stdout="active\n")])
    clock = Clock()
    result = enable_and_start(
        tmp_path, run=run, confirm=lambda _step: True, now=lambda: "2026-10-13T00:00:00Z",
        api_status=lambda: (_ for _ in ()).throw(FileNotFoundError()),
        features=["web"], ops_ready=lambda _feature: False,
        clock=clock, sleep=clock.sleep,
    )
    assert result.active == "ok"
    assert result.api_result == "failed" and result.api_reason_code == "socket_missing"
    assert result.ops == {"web": "failed"}


def test_start_marks_api_disabled_without_connecting(tmp_path):
    result = enable_and_start(
        tmp_path, run=Run([Completed(), Completed(), Completed(stdout="-- cursor: before\n"), Completed(), Completed(stdout="new\n"), Completed(stdout="active\n")]),
        confirm=lambda _step: True, now=lambda: "2026-10-13T00:00:00Z", api_enabled=False,
        api_status=lambda: (_ for _ in ()).throw(AssertionError("API must not be contacted")),
    )
    assert (result.api_result, result.api_reason_code) == ("disabled", "disabled")


def _line(invocation, message, *, user=False):
    field = "USER_INVOCATION_ID" if user else "_SYSTEMD_INVOCATION_ID"
    return json.dumps({field: invocation, "MESSAGE": message})


def test_admission_uses_matching_invocation_and_last_valid_line():
    run = Run([Completed(stdout="\n".join([
        _line("old", "improve isolation: enabled; backend=claude admitted=yes fingerprint=a"),
        _line("new", "improve isolation: enabled; backend=claude admitted=no reason=sandbox_unavailable fingerprint=b"),
        _line("new", "improve isolation: enabled; backend=claude admitted=yes fingerprint=c"),
    ]))])
    observed = read_admission_line(run, "new", cursor_fallback="cursor", backend="claude")
    assert (observed.admitted, observed.reason_code, observed.method) == ("yes", "invocation_id", "invocation_id")
    assert not any("_SYSTEMD_INVOCATION_ID=" in " ".join(call) for call in run.calls)
    assert run.calls[0][run.calls[0].index("--after-cursor") + 1] == "cursor"
    assert "-n" not in run.calls[0]


def test_admission_reads_the_last_entries_when_no_cursor_was_obtained():
    run = Run([Completed(stdout=_line("new", "improve isolation: enabled; backend=claude admitted=yes fingerprint=a"))])
    observed = read_admission_line(run, "new", cursor_fallback=None, backend="claude")
    assert observed.admitted == "yes"
    assert run.calls[0][-2:] == ["-n", "5000"] and "--after-cursor" not in run.calls[0]


def test_admission_falls_back_to_user_field_then_cursor_only_when_fields_absent():
    user_run = Run([Completed(stdout=_line("new", "improve isolation: enabled; backend=claude admitted=no reason=sandbox_unavailable fingerprint=a", user=True))])
    user = read_admission_line(user_run, "new", cursor_fallback="cursor", backend="claude")
    assert (user.admitted, user.reason_code, user.method) == ("no", "sandbox_unavailable", "invocation_id")

    cursor_run = Run([Completed(stdout=json.dumps({"MESSAGE": "other"})),
                      Completed(stdout=json.dumps({"MESSAGE": "improve isolation: enabled; backend=claude admitted=yes fingerprint=a"}))])
    cursor = read_admission_line(cursor_run, "new", cursor_fallback="cursor", backend="claude")
    assert (cursor.admitted, cursor.reason_code, cursor.method) == ("yes", "cursor", "cursor")
    assert "--after-cursor" in cursor_run.calls[1]


def test_admission_missing_or_malformed_is_unknown_with_distinct_reason():
    missing = read_admission_line(Run([Completed(stdout="")]), "new", cursor_fallback="cursor", backend="claude")
    malformed = read_admission_line(Run([Completed(stdout="not-json")]), "new", cursor_fallback="cursor", backend="claude")
    assert (missing.admitted, missing.reason_code) == ("unknown", "admission_line_timeout")
    assert (malformed.admitted, malformed.reason_code) == ("unknown", "journal_format_invalid")


def test_admission_reports_journal_unavailable_after_cursor_fallback_fails():
    observed = read_admission_line(Run([OSError(), OSError()]), "new",
                                   cursor_fallback="cursor", backend="claude")
    assert (observed.admitted, observed.reason_code) == ("unknown", "journal_unavailable")


def test_handoff_does_not_create_marker_or_enable_when_ops_is_unavailable(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()
    calls: list[str] = []
    try:
        outcomes = handoff_and_enable(
            tmp_path, lease, ["web", "bot"], now=lambda: "2026-10-13T00:00:00Z",
            enable=lambda *_args, **_kwargs: calls.append("enable"),
            precheck=lambda _root: type("Check", (), {"available": False, "reason_code": "ops_exec_not_found"})(),
        )
        assert calls == []
        assert not setup_marker_present(tmp_path)
        assert [(item.feature, item.result, item.reason_code) for item in outcomes] == [
            ("web", "later", "ops_exec_not_found"), ("bot", "later", "ops_exec_not_found")]
    finally:
        lease.close()


@pytest.mark.parametrize(("boundary", "marker_remains"), [
    ("after_handoff_marker", True),
    ("before_handoff_instance_lock_release", True),
    ("enable", True),
    ("before_setup_marker_remove", True),
    ("after_setup_marker_unlink", False),
])
def test_handoff_preserves_the_expected_marker_state_at_each_interrupt_boundary(tmp_path, boundary, marker_remains):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()

    def hook(name):
        if name == boundary:
            raise RuntimeError(boundary)

    def enable(*_args, **_kwargs):
        if boundary == "enable":
            raise RuntimeError(boundary)
        return EnableResult(0, "enable", "ok")

    try:
        with pytest.raises(RuntimeError, match=boundary):
            handoff_and_enable(
                tmp_path, lease, ["web"], enable=enable, hook=hook,
                now=lambda: "2026-10-13T00:00:00Z",
                precheck=lambda _root: type("Check", (), {"available": True})(),
            )
        assert setup_marker_present(tmp_path) is marker_remains
    finally:
        lease.close()


@pytest.mark.parametrize("reason_code", sorted(__import__("agentic_fx.ops_setup.interactive", fromlist=["REASON_CODES"]).REASON_CODES)
                         + ["feature_config_unsafe:symlink", "feature_config_unsafe:web_config",
                            "feature_probe_failed:connect_error", "feature_probe_failed:timeout"])
def test_enable_reason_codes_are_valid_setup_record_values(reason_code):
    from agentic_fx.setup.post_start import FeatureOutcome

    values = ops_record_values([FeatureOutcome("web", "failed", "enable", reason_code, "2026-10-13T00:00:00Z")])
    record = {
        "version": 1, "completed_at": "2026-10-13T00:00:00Z", "mode": "interactive",
        "settings_schema_version": 1, "price": {"result": "ok"}, "llm_trade": {"result": "ok"},
        "llm_improve": {"result": "ok"}, "bridge_plan": "unused", "history": {"source": "none"},
        "indicators": {"installed": 0}, "api": {"enabled": False, "result": "disabled"},
        "ops": values, "isolation": {"admitted": "unknown"}, "linger": {"state": "declined"},
        "start": {"result": "skipped"}, "unit": "not_installed",
    }
    assert _valid(record)


def test_admission_waits_for_current_invocation_when_only_an_old_one_has_the_field():
    clock = Clock()
    old = _line("old", "improve isolation: enabled; backend=claude admitted=yes fingerprint=old")

    def run(argv, **_kwargs):
        run.calls.append(list(argv))
        return Completed(stdout=old)
    run.calls = []
    observed = read_admission_line(run, "new", cursor_fallback="cursor", backend="claude",
                                   clock=clock, sleep=clock.sleep, timeout=6)
    assert (observed.admitted, observed.reason_code, observed.method) == ("unknown", "admission_line_timeout", "invocation_id")
    assert all(call.count("--after-cursor") == 1 for call in run.calls)
    assert clock.sleeps == [2.0, 2.0, 2.0] and len(run.calls) == 4


def test_admission_uses_cursor_when_no_entry_has_an_invocation_field():
    run = Run([Completed(stdout=json.dumps({"MESSAGE": "unrelated"})),
               Completed(stdout=json.dumps({"MESSAGE": "improve isolation: enabled; backend=claude admitted=no reason=sandbox_unavailable fingerprint=c"}))])
    observed = read_admission_line(run, "new", cursor_fallback="cursor", backend="claude")
    assert (observed.admitted, observed.reason_code, observed.method) == ("no", "sandbox_unavailable", "cursor")


def test_admission_is_not_observed_for_a_local_backend():
    line = ("improve isolation: enabled; backend=local is not a CLI backend; cgroup_gate=open")
    run = Run([Completed(stdout=_line("new", line))])
    assert read_admission_line(run, "new", cursor_fallback="cursor", backend="local") is None
    assert run.calls == []
    assert isolation_record_values(None, at="2026-10-13T00:00:00Z") == {
        "admitted": "unknown", "reason_code": "not_cli_backend", "at": "2026-10-13T00:00:00Z"}


def test_admission_rejects_extra_words_after_fingerprint():
    observed = read_admission_line(
        Run([Completed(stdout=_line("new", "improve isolation: enabled; backend=claude admitted=yes fingerprint=a extra"))]),
        "new", cursor_fallback="cursor", backend="claude",
    )
    assert (observed.admitted, observed.reason_code) == ("unknown", "journal_format_invalid")


def test_start_waits_for_new_invocation_and_observes_later_layers_after_active_failure(tmp_path):
    clock = Clock()

    def run(argv, **_kwargs):
        if argv[:3] == ["systemctl", "--user", "show"]:
            return Completed(stdout="old\n" if not hasattr(run, "shown") else "new\n")
        if argv[:3] == ["journalctl", "--user", "-n"]:
            return Completed(stdout="-- cursor: before\n")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return Completed(stdout="failed\n", returncode=3)
        if argv[:3] == ["systemctl", "--user", "start"]:
            run.shown = True
        return Completed()

    result = enable_and_start(
        tmp_path, run=run, confirm=lambda _step: True, now=lambda: "2026-10-13T00:00:00Z",
        api_status=lambda: None, features=["web"], ops_ready=lambda _feature: True,
        clock=clock, sleep=clock.sleep,
    )
    assert result.reason_code == "service_not_active"
    assert result.active == "failed" and result.api_result == "ok" and result.ops == {"web": "ok"}
    assert result.invocation_id == "new" and result.journal_cursor == "before"
    assert clock.sleeps == []


def test_start_retries_active_api_and_ops_with_bounded_injected_time(tmp_path):
    clock = Clock()
    active = iter([Completed(stdout="activating\n"), Completed(stdout="active\n")])
    api = iter([FileNotFoundError(), None])
    ready = iter([False, True])

    def run(argv, **_kwargs):
        if argv[:3] == ["systemctl", "--user", "show"]:
            return Completed(stdout="\n" if not hasattr(run, "started") else "new\n")
        if argv[:3] == ["journalctl", "--user", "-n"]:
            return Completed(stdout="-- cursor: before\n")
        if argv[:3] == ["systemctl", "--user", "start"]:
            run.started = True
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return next(active)
        return Completed()

    def api_status():
        value = next(api)
        if isinstance(value, BaseException):
            raise value

    result = enable_and_start(
        tmp_path, run=run, confirm=lambda _step: True, now=lambda: "2026-10-13T00:00:00Z",
        api_status=api_status, features=["web"], ops_ready=lambda _feature: next(ready),
        clock=clock, sleep=clock.sleep,
    )
    assert (result.active, result.api_result, result.ops) == ("ok", "ok", {"web": "ok"})
    assert clock.sleeps == [0.5, 2.0, 2.0]


def _start_run():
    def run(argv, **_kwargs):
        if argv[:3] == ["systemctl", "--user", "show"]:
            return Completed(stdout="new\n" if getattr(run, "started", False) else "\n")
        if argv[:3] == ["journalctl", "--user", "-n"]:
            return Completed(stdout="-- cursor: before\n")
        if argv[:3] == ["systemctl", "--user", "start"]:
            run.started = True
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return Completed(stdout="activating\n", returncode=3)
        return Completed()
    return run


def _start(tmp_path, clock, **kwargs):
    return enable_and_start(tmp_path, run=_start_run(), confirm=lambda _step: True,
                            now=lambda: "2026-10-13T00:00:00Z", clock=clock, sleep=clock.sleep, **kwargs)


def test_start_never_active_gives_up_at_ten_seconds(tmp_path):
    clock = Clock()
    result = _start(tmp_path, clock, api_status=lambda: None)
    assert (result.result, result.reason_code, result.active) == ("pending", "service_not_active", "failed")
    # the new invocation is visible at once; activation is polled every 0.5 s
    assert clock.sleeps == [0.5] * 20 and clock.value == 10.0


def test_start_api_always_failing_gives_up_at_thirty_seconds_with_its_reason(tmp_path):
    clock = Clock()
    attempts = []

    def api_status():
        attempts.append(clock.value)
        raise FileNotFoundError

    result = _start(tmp_path, clock, api_status=api_status)
    assert (result.api_result, result.api_reason_code) == ("failed", "socket_missing")
    assert attempts == [10.0 + 2.0 * n for n in range(16)]
    assert clock.sleeps == [0.5] * 20 + [2.0] * 15 and clock.value == 40.0


def test_start_ops_never_ready_gives_up_at_thirty_seconds_as_failed(tmp_path):
    clock = Clock()
    checks = []
    result = _start(tmp_path, clock, api_status=lambda: None, features=["web"],
                    ops_ready=lambda feature: checks.append(clock.value) or False)
    assert result.ops == {"web": "failed"}
    assert checks == [10.0 + 2.0 * n for n in range(16)]
    assert clock.sleeps == [0.5] * 20 + [2.0] * 15 and clock.value == 40.0
    assert result.reason_code == "service_not_active"


def _complete_record(isolation):
    record = {
        "version": 1, "completed_at": "2026-10-13T00:00:00Z", "mode": "interactive",
        "settings_schema_version": 1, "price": {"result": "ok"}, "llm_trade": {"result": "ok"},
        "llm_improve": {"result": "ok"}, "bridge_plan": "unused", "history": {"source": "none"},
        "indicators": {"installed": 0}, "api": {"enabled": False, "result": "disabled"},
        "ops": {}, "linger": {"state": "declined"}, "start": {"result": "skipped"},
        "unit": "not_installed",
    }
    if isolation is not None:
        record["isolation"] = isolation
    return record


def test_setup_record_requires_isolation():
    assert not _valid(_complete_record(None))


def test_setup_record_accepts_the_fixed_isolation_value_of_a_non_cli_backend():
    values = isolation_record_values(None, at="2026-10-13T00:00:00Z")
    assert _valid(_complete_record(values))


def test_the_handoff_marker_names_its_phase_and_start_time(tmp_path):
    from agentic_fx.store.setup_marker import read_setup_marker

    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()
    seen = []

    def enable(root, feature, *, lease=None):
        seen.append(read_setup_marker(tmp_path))
        return EnableResult(0, "enabled", None)

    try:
        handoff_and_enable(tmp_path, lease, ["bot"], enable=enable,
                           precheck=lambda root: __import__("agentic_fx.setup.post_start", fromlist=["x"]).OpsPrecheck(True),
                           now=lambda: "2026-10-02T06:00:00+00:00")
    finally:
        lease.close()
    assert seen[0].phase == "handoff" and seen[0].started_at == "2026-10-02T06:00:00+00:00"
    assert seen[0].settings_sha256 is None

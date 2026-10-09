"""admission の契約のうち、既存のテストが値で固定していない部分。

固定 reason の語、systemctl に渡す env と stdin、出力上限と行の文法、子 process の後始末。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agentic_fx.core import unit_admission as ua

from . import test_unit_admission


def _good_props() -> dict[str, str]:
    return {
        "Delegate": "yes",
        "DelegateControllers": "memory pids",
        "MainPID": "4242",
        "Type": "exec",
        "NotifyAccess": "none",
        "KillMode": "control-group",
        "TimeoutStopUSec": "5s",
        "SendSIGKILL": "yes",
        "FinalKillSignal": "9",
        "TimeoutStopFailureMode": "terminate",
        "TimeoutAbortUSec": "5s",
    }


def _show_bytes(props: dict[str, str]) -> bytes:
    return "".join(f"{k}={v}\n" for k, v in props.items()).encode()


def _script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


def test_admission_reason_words_are_fixed():
    assert ua.REASON_DELEGATION_MISSING == "cgroup_delegation_missing"
    assert ua.REASON_TOPOLOGY_UNSUPPORTED == "cgroup_topology_unsupported"
    assert ua.REASON_STOP_POLICY_UNSUPPORTED == "unit_stop_policy_unsupported"
    assert ua.ADMISSION_REASONS == frozenset({
        "cgroup_delegation_missing", "cgroup_topology_unsupported",
        "unit_stop_policy_unsupported"})


def test_run_show_child_sees_only_sanitized_env_and_null_stdin(tmp_path, monkeypatch):
    """systemctl に見えるのは sanitized env だけで、stdin は呼び出し元の fd 0 でなく /dev/null。"""
    monkeypatch.setenv("HOME", "/home/leak")
    monkeypatch.setenv("LD_PRELOAD", "leak.so")
    script = _script(tmp_path, "probe.sh",
                     'echo "HOME=${HOME-unset}"\n'
                     'echo "LD_PRELOAD=${LD_PRELOAD-unset}"\n'
                     'echo "PATH=$PATH"\n'
                     'echo "SYSTEMD_PAGER=${SYSTEMD_PAGER-unset}"\n'
                     'echo "STDIN=$(readlink /proc/$$/fd/0)"\n')
    # 呼び出し元の fd 0 を pipe にして、継承されたら見分けられるようにする
    saved = os.dup(0)
    r, w = os.pipe()
    try:
        os.dup2(r, 0)
        raw = ua.run_show([script], ua.sanitized_env())
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(r)
        os.close(w)
    lines = dict(line.split("=", 1) for line in raw.decode().splitlines())
    assert lines["HOME"] == "unset"
    assert lines["LD_PRELOAD"] == "unset"
    assert lines["PATH"] == "/usr/bin:/bin"
    assert lines["SYSTEMD_PAGER"] == ""
    assert lines["STDIN"] == "/dev/null"


def test_show_output_parser_rejects_well_formed_output_over_limit():
    props = _good_props()
    props["DelegateControllers"] = "memory pids" + " cpu" * 1100
    raw = _show_bytes(props)
    assert len(raw) > ua.SHOW_STDOUT_LIMIT
    with pytest.raises(ua.AdmissionError) as ei:
        ua.parse_show_output(raw)
    assert ei.value.reason == ua.REASON_DELEGATION_MISSING
    assert str(ua.SHOW_STDOUT_LIMIT) in ei.value.detail


def test_show_output_parser_rejects_line_without_equals_even_if_key_is_known():
    raw = _show_bytes(_good_props()).replace(b"KillMode=control-group\n", b"KillMode\n")
    with pytest.raises(ua.AdmissionError) as ei:
        ua.parse_show_output(raw)
    assert ei.value.reason == ua.REASON_DELEGATION_MISSING
    assert "=" in ei.value.detail


def _alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return st.rsplit(")", 1)[1].split()[0] != "Z"


def test_run_show_times_out_when_child_closes_stdout_but_keeps_running(tmp_path):
    pidfile = tmp_path / "pid"
    script = _script(tmp_path, "closer.sh", f'echo $$ > {pidfile}\nexec >&-\nexec sleep 8\n')
    t0 = time.monotonic()
    try:
        with pytest.raises(ua.AdmissionError) as ei:
            ua.run_show([script], ua.sanitized_env())
        elapsed = time.monotonic() - t0
        assert ei.value.reason == ua.REASON_DELEGATION_MISSING
        assert "timeout" in ei.value.detail
        assert 4.5 <= elapsed < 6.5, elapsed
        assert not _alive(int(pidfile.read_text())), "timeout の後に子が残っている"
    finally:
        if pidfile.exists() and _alive(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), 9)


def test_run_show_kills_and_reaps_child_on_timeout(tmp_path):
    pidfile = tmp_path / "pid"
    script = _script(tmp_path, "slow.sh", f'echo $$ > {pidfile}\nexec sleep 8\n')
    try:
        with pytest.raises(ua.AdmissionError):
            ua.run_show([script], ua.sanitized_env())
        pid = int(pidfile.read_text())
        assert not Path(f"/proc/{pid}").exists(), "timeout の後の子が kill / reap されていない"
    finally:
        if pidfile.exists() and _alive(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), 9)


def test_show_output_parser_rejects_cr_inside_value():
    props = _good_props()
    props["DelegateControllers"] = "memory pids x\ry"
    with pytest.raises(ua.AdmissionError) as ei:
        ua.parse_show_output(_show_bytes(props))
    assert ei.value.reason == ua.REASON_DELEGATION_MISSING


@pytest.mark.parametrize("mode", [0o100775, 0o100757, 0o100777])
def test_root_owned_systemctl_writable_by_group_or_other_is_rejected(monkeypatch, mode):
    real = os.lstat("/usr/bin/systemctl" if os.path.lexists("/usr/bin/systemctl")
                    else "/bin/systemctl")
    fields = list(real)
    fields[0] = mode   # st_mode
    fields[4] = 0      # st_uid
    fake = os.stat_result(fields)
    monkeypatch.setattr(ua.os, "lstat", lambda path: fake)
    with pytest.raises(ua.AdmissionError) as ei:
        ua.check_systemctl_executable("/usr/bin/systemctl")
    assert ei.value.reason == ua.REASON_DELEGATION_MISSING
    assert "書込み可能" in ei.value.detail
    fields[0] = 0o100755
    monkeypatch.setattr(ua.os, "lstat", lambda path: os.stat_result(fields))
    ua.check_systemctl_executable("/usr/bin/systemctl")


# --- duration の整数解析 ---

@pytest.mark.parametrize("text,usec", [
    ("1.5s", 1_500_000),
    ("1us", 1), ("1µs", 1), ("1ms", 1_000), ("1s", 1_000_000), ("1min", 60_000_000),
    ("1h", 3_600_000_000), ("1d", 86_400_000_000), ("1w", 604_800_000_000),
    ("1month", 2_629_800_000_000), ("1y", 31_557_600_000_000),
    ("0.5ms", 500), ("0.0005ms", 0), ("0.999us", 0),
    ("1min 30.5s", 90_500_000),
    ("99999999999999999999y", 99999999999999999999 * 31_557_600_000_000),
    ("0.1s", 100_000), ("0.3s", 300_000),
])
def test_timespan_is_exact_integer_arithmetic(text, usec):
    assert ua.parse_timespan_usec(text) == usec


@pytest.mark.parametrize("text", [
    "999999999999999999999y",
    "9" * 400 + "s",
    "1." + "0" * 21 + "s",
    "1e400s", "inf s", "infs", "nan s", "nans", "1e3s", "-1s", "+1s", ".5s", "1.s", "0x10s",
    "１s",
])
def test_timespan_rejects_oversized_and_non_decimal_forms(text):
    with pytest.raises(ValueError):
        ua.parse_timespan_usec(text)


@pytest.mark.parametrize("key", ["TimeoutStopUSec", "TimeoutAbortUSec"])
@pytest.mark.parametrize("value", ["9" * 400 + "s", "999999999999999999999y", "inf s"])
def test_oversized_duration_maps_to_fixed_reason(key, value):
    props = test_unit_admission._good_props()
    props[key] = value
    with pytest.raises(ua.AdmissionError) as ei:
        ua.judge_properties(props, 4242)
    assert ei.value.reason == ua.REASON_STOP_POLICY_UNSUPPORTED


@pytest.mark.parametrize("key", ["TimeoutStopUSec", "TimeoutAbortUSec"])
def test_overflow_error_from_duration_parser_maps_to_fixed_reason(key, monkeypatch):
    def boom(text):
        raise OverflowError("int too large to convert to float")
    monkeypatch.setattr(ua, "parse_timespan_usec", boom)
    props = test_unit_admission._good_props()
    with pytest.raises(ua.AdmissionError) as ei:
        ua.judge_properties(props, 4242)
    assert ei.value.reason == ua.REASON_STOP_POLICY_UNSUPPORTED

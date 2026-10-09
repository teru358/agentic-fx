"""unit の同定と admission (`systemctl --user show` の固定問い合わせと property の判定)。

libsystemd・systemctl・systemd の停止動作はモックしない。正の経路は使い捨ての user
service の中で helper を走らせて観測する (`_disposable_unit.py`)。
"""
from __future__ import annotations

import ctypes
import json
import os
import stat
import time
from pathlib import Path

import pytest

from agentic_fx.core import unit_admission as ua
from tests.core._disposable_unit import (  # noqa: F401 (fixture)
    disposable_units,
    systemd_environment,
)

DELEGATION = ua.REASON_DELEGATION_MISSING
TOPOLOGY = ua.REASON_TOPOLOGY_UNSUPPORTED
STOP = ua.REASON_STOP_POLICY_UNSUPPORTED


def _good_props(pid: int = 4242) -> dict[str, str]:
    return {
        "Delegate": "yes",
        "DelegateControllers": "memory pids",
        "MainPID": str(pid),
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


# ------------------------------------------------------------------ libsystemd


def _in_login_session_scope() -> bool:
    text = Path("/proc/self/cgroup").read_text()
    return ".scope" in text and "/session-" in text


def test_session_scope_has_no_user_unit(monkeypatch):
    if not _in_login_session_scope():
        pytest.skip("pytest が login session の scope の中で走っていない (user unit の中など)")
    calls: list[list[str]] = []
    original = ua.run_show

    def spy(argv, env, **kw):
        calls.append(list(argv))
        return original(argv, env, **kw)

    monkeypatch.setattr(ua, "run_show", spy)
    with pytest.raises(ua.AdmissionError) as ei:
        ua.identify_user_unit()
    assert ei.value.reason == DELEGATION
    assert "ENODATA" in ei.value.detail
    with pytest.raises(ua.AdmissionError) as ei2:
        ua.admit_unit()
    assert ei2.value.reason == DELEGATION
    assert calls == [], "unit が得られないのに systemctl を起こした"


def test_missing_library_and_symbol_fail_closed():
    with pytest.raises(ua.AdmissionError) as ei:
        ua.identify_user_unit(library="libsystemd.so.999")
    assert ei.value.reason == DELEGATION
    assert "OSError" in ei.value.detail
    try:
        ctypes.CDLL("libsystemd.so.0")
    except OSError:
        pytest.skip("libsystemd.so.0 が無い")
    with pytest.raises(ua.AdmissionError) as ei:
        ua.identify_user_unit(symbol="sd_pid_get_user_unit_no_such_symbol")
    assert ei.value.reason == DELEGATION
    assert "AttributeError" in ei.value.detail


class _FreeRecorder:
    def __init__(self):
        self.calls: list[int] = []

    def __call__(self, ptr):
        self.calls.append(ptr.value if isinstance(ptr, ctypes.c_void_p) else ptr)


@pytest.mark.parametrize("raw", [
    None,                          # rc >= 0 なのに null
    b"\xff\xfe.service",           # UTF-8 でない
    b"afx",                        # .service で終わらない
    b"afx.scope",
    b".service",                   # 名前が空
    b"a/b.service",                # / を含む
    b"afx\x01.service",            # 制御文字
    b"afx x.service",              # 空白
    b"afx@.service",               # template そのもの (実行中の unit にはならない)
    b"a@b@c.service",
    b"a" * 300 + b".service",      # 長すぎる
])
def test_pointer_result_handling(raw):
    free = _FreeRecorder()
    keep = None
    if raw is None:
        ptr = None
    else:
        keep = ctypes.create_string_buffer(raw)
        ptr = ctypes.addressof(keep)
    with pytest.raises(ua.AdmissionError) as ei:
        ua.unit_name_from_result(0, ptr, free=free)
    assert ei.value.reason == DELEGATION
    if ptr is None:
        assert free.calls == []
    else:
        assert free.calls == [ptr], "非 null pointer は失敗時も一度だけ free する"


def test_pointer_result_handling_negative_rc_and_success():
    free = _FreeRecorder()
    with pytest.raises(ua.AdmissionError) as ei:
        ua.unit_name_from_result(-61, None, free=free)
    assert ei.value.reason == DELEGATION
    assert "ENODATA" in ei.value.detail
    assert free.calls == []

    buf = ctypes.create_string_buffer(b"leftover.service")
    with pytest.raises(ua.AdmissionError):
        ua.unit_name_from_result(-5, ctypes.addressof(buf), free=free)
    assert free.calls == [ctypes.addressof(buf)]

    free = _FreeRecorder()
    buf = ctypes.create_string_buffer(b"afx-test@inst_1.2:x\\x2d.service")
    name = ua.unit_name_from_result(0, ctypes.addressof(buf), free=free)
    assert name == "afx-test@inst_1.2:x\\x2d.service"
    assert free.calls == [ctypes.addressof(buf)], "成功時に free が一度だけ呼ばれる"


# ------------------------------------------------------------------ systemctl


def test_systemctl_executable_must_be_root_owned(tmp_path):
    real = Path("/usr/bin/systemctl")
    if not real.exists():
        real = Path("/bin/systemctl")
    if not real.exists():
        pytest.skip("systemctl が無い")
    ua.check_systemctl_executable(str(real))
    assert ua.locate_systemctl() in ("/usr/bin/systemctl", "/bin/systemctl")

    mine = tmp_path / "systemctl"
    mine.write_text("#!/bin/sh\n")
    mine.chmod(0o755)
    with pytest.raises(ua.AdmissionError) as ei:
        ua.check_systemctl_executable(str(mine))
    assert ei.value.reason == DELEGATION

    gw = tmp_path / "gw"
    gw.write_text("#!/bin/sh\n")
    gw.chmod(0o775)
    assert gw.stat().st_mode & stat.S_IWGRP
    with pytest.raises(ua.AdmissionError):
        ua.check_systemctl_executable(str(gw))

    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ua.AdmissionError) as ei:
        ua.check_systemctl_executable(str(link))
    assert "symlink" in ei.value.detail

    with pytest.raises(ua.AdmissionError):
        ua.check_systemctl_executable(str(tmp_path / "absent"))
    with pytest.raises(ua.AdmissionError):
        ua.check_systemctl_executable(str(tmp_path))

    # 先頭候補が無いときだけ次へ。不合格の先頭候補を飛ばして次へは行かない
    assert ua.locate_systemctl((str(tmp_path / "absent"), str(real))) == str(real)
    with pytest.raises(ua.AdmissionError):
        ua.locate_systemctl((str(mine), str(real)))
    with pytest.raises(ua.AdmissionError):
        ua.locate_systemctl((str(tmp_path / "absent"),))


def test_fixed_argv_and_sanitized_env():
    argv = ua.show_argv("/usr/bin/systemctl", "afx.service")
    assert argv == [
        "/usr/bin/systemctl", "--user", "--no-pager", "show",
        "-p", "Delegate", "-p", "DelegateControllers", "-p", "MainPID",
        "-p", "Type", "-p", "NotifyAccess", "-p", "KillMode",
        "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "FinalKillSignal",
        "-p", "TimeoutStopFailureMode", "-p", "TimeoutAbortUSec", "--", "afx.service",
    ]
    env = ua.sanitized_env()
    assert env == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "SYSTEMD_PAGER": "",
        "PAGER": "cat",
        "SYSTEMD_LOG_LEVEL": "err",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
    }
    for leaked in ("HOME", "DBUS_SESSION_BUS_ADDRESS", "SYSTEMD_COLORS", "LD_PRELOAD",
                   "PYTHONPATH", "USER"):
        assert leaked not in env
    assert ua.sanitized_env(uid=1234)["XDG_RUNTIME_DIR"] == "/run/user/1234"


_HOSTILE = {
    "duplicate_key": _show_bytes(_good_props()) + b"Delegate=yes\n",
    "unknown_key": _show_bytes(_good_props()) + b"NoSuchProperty=1\n",
    "no_equals": _show_bytes(_good_props()) + b"Delegate\n",
    "empty": b"",
    "only_newline": b"\n",
    "missing_key": _show_bytes({k: v for k, v in _good_props().items() if k != "KillMode"}),
    "too_large": _show_bytes(_good_props()) + b"X" * 4096,
    "not_utf8": _show_bytes(_good_props()).replace(b"Type=exec", b"Type=ex\xffec"),
    "nul": _show_bytes(_good_props()).replace(b"Type=exec", b"Type=exec\x00"),
    "cr": _show_bytes(_good_props()).replace(b"\n", b"\r\n"),
    "blank_line_in_middle": _show_bytes(_good_props()).replace(b"\nType", b"\n\nType"),
    "leading_space_value": _show_bytes(_good_props()).replace(b"Type=exec", b"Type= exec"),
    "trailing_space_value": _show_bytes(_good_props()).replace(b"Type=exec", b"Type=exec "),
    "space_before_key": _show_bytes(_good_props()).replace(b"Type=exec", b" Type=exec"),
    "tab_value": _show_bytes(_good_props()).replace(b"Type=exec", b"Type=exec\t"),
}


@pytest.mark.parametrize("case", sorted(_HOSTILE))
def test_show_output_parser_rejects_hostile_output(case):
    with pytest.raises(ua.AdmissionError) as ei:
        ua.parse_show_output(_HOSTILE[case])
    assert ei.value.reason == DELEGATION


def test_show_output_parser_accepts_real_shape():
    raw = _show_bytes(_good_props())
    assert ua.parse_show_output(raw) == _good_props()
    # 末尾改行が無くても同じ
    assert ua.parse_show_output(raw.rstrip(b"\n")) == _good_props()


_PROPERTY_CASES = [
    ("Delegate", "no", DELEGATION),
    ("Delegate", "", DELEGATION),
    ("DelegateControllers", "memory", DELEGATION),
    ("DelegateControllers", "pids", DELEGATION),
    ("DelegateControllers", "", DELEGATION),
    ("MainPID", "1", TOPOLOGY),
    ("MainPID", "0", TOPOLOGY),
    ("MainPID", "04242", TOPOLOGY),
    ("MainPID", "+4242", TOPOLOGY),
    ("Type", "simple", STOP),
    ("Type", "notify", STOP),
    ("Type", "forking", STOP),
    ("NotifyAccess", "main", STOP),
    ("NotifyAccess", "all", STOP),
    ("NotifyAccess", "exec", STOP),
    ("KillMode", "mixed", STOP),
    ("KillMode", "process", STOP),
    ("KillMode", "none", STOP),
    ("TimeoutStopUSec", "infinity", STOP),
    ("TimeoutStopUSec", "0", STOP),
    ("TimeoutStopUSec", "5", STOP),
    ("TimeoutStopUSec", "-5s", STOP),
    ("TimeoutStopUSec", "5 s", STOP),
    ("TimeoutStopUSec", "", STOP),
    ("SendSIGKILL", "no", STOP),
    ("FinalKillSignal", "15", STOP),
    ("FinalKillSignal", "SIGKILL", STOP),
    ("TimeoutStopFailureMode", "abort", STOP),
    ("TimeoutStopFailureMode", "", STOP),
    ("TimeoutAbortUSec", "soon", STOP),
    ("TimeoutAbortUSec", "5x", STOP),
    ("TimeoutAbortUSec", "", STOP),
    ("TimeoutAbortUSec", "1min30", STOP),
]


@pytest.mark.parametrize("key,value,reason", _PROPERTY_CASES)
def test_property_values(key, value, reason):
    props = _good_props()
    props[key] = value
    with pytest.raises(ua.AdmissionError) as ei:
        ua.judge_properties(props, 4242)
    assert ei.value.reason == reason


@pytest.mark.parametrize("key,value", [
    ("DelegateControllers", "cpu memory pids io"),
    ("DelegateControllers", "pids memory"),
    ("TimeoutStopUSec", "1min 30s"),
    ("TimeoutStopUSec", "2h 5min 1s 500ms"),
    ("TimeoutStopUSec", "1us"),
    ("TimeoutStopFailureMode", "kill"),
    ("TimeoutAbortUSec", "infinity"),
    ("TimeoutAbortUSec", "0"),
    ("TimeoutAbortUSec", "1min 30s"),
])
def test_property_values_accepted(key, value):
    props = _good_props()
    props[key] = value
    ua.judge_properties(props, 4242)


def test_property_order_delegation_before_topology_before_stop():
    props = _good_props()
    props.update(Delegate="no", MainPID="1", Type="simple")
    with pytest.raises(ua.AdmissionError) as ei:
        ua.judge_properties(props, 4242)
    assert ei.value.reason == DELEGATION
    props["Delegate"] = "yes"
    with pytest.raises(ua.AdmissionError) as ei:
        ua.judge_properties(props, 4242)
    assert ei.value.reason == TOPOLOGY


def test_slow_or_flooding_systemctl_fails_closed(tmp_path):
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nexec sleep 6\n")
    slow.chmod(0o755)
    flood = tmp_path / "flood.sh"
    flood.write_text("#!/bin/sh\nhead -c 5120 /dev/zero | tr '\\0' 'A'\nexec sleep 6\n")
    flood.chmod(0o755)
    fail = tmp_path / "fail.sh"
    fail.write_text("#!/bin/sh\necho Delegate=yes\nexit 1\n")
    fail.chmod(0o755)

    t0 = time.monotonic()
    with pytest.raises(ua.AdmissionError) as ei:
        ua.run_show([str(slow)], ua.sanitized_env())
    elapsed = time.monotonic() - t0
    assert ei.value.reason == DELEGATION
    assert "timeout" in ei.value.detail
    assert 4.5 <= elapsed < 5.9, elapsed

    t0 = time.monotonic()
    with pytest.raises(ua.AdmissionError) as ei:
        ua.run_show([str(flood)], ua.sanitized_env())
    assert ei.value.reason == DELEGATION
    assert "4096" in ei.value.detail
    assert time.monotonic() - t0 < 3, "上限超過は timeout を待たずに打ち切る"

    with pytest.raises(ua.AdmissionError) as ei:
        ua.run_show([str(fail)], ua.sanitized_env())
    assert ei.value.reason == DELEGATION

    # 正の対照: 実 systemctl は manager に届けば 0 で返る
    assert ua.SHOW_TIMEOUT_SEC == 5
    assert ua.SHOW_STDOUT_LIMIT == 4096


# ------------------------------------------------------------------ 使い捨て service


def test_disposable_service_passes_admission_and_self_moves(disposable_units):
    res = disposable_units.run_helper("ok", "admit_and_move")
    unit = disposable_units.unit_name("ok")
    assert res.get("error") is None, res
    adm = res["admission"]
    assert adm["ok"] is True, adm
    assert adm["unit"] == unit
    assert adm["properties"]["MainPID"] == str(res["pid"])
    assert adm["properties"]["Delegate"] == "yes"
    assert set(adm["properties"]) == set(ua.SHOW_PROPERTIES)
    topo = res["topology"]
    assert topo["root_procs_before"] == [str(res["pid"])]
    assert topo["self_cgroup_after"].endswith(f"/{unit}/supervisor")
    assert topo["root_procs_after"] == []
    assert set(topo["root_subtree_control_after"].split()) >= {"pids", "memory"}
    child = res["mission_cgroup"]
    assert child["pids.max"] == "64"
    assert child["memory.max"] == str(256 << 20)
    assert child["cleanup_reason"] is None
    assert child["exists_after"] is False
    assert res["gate"]["mission_allowed_after_cleanup"] is True
    assert res["topology_again"]["ok"] is True, "同じ process での再実行は冪等"
    assert disposable_units.listed() == []


def test_shell_wrapper_service_is_topology_unsupported(disposable_units):
    res = disposable_units.run_helper("wrap", "admit_and_move", wrapper=True)
    assert res.get("error") is None, res
    adm = res["admission"]
    assert adm["ok"] is False
    assert adm["reason"] == TOPOLOGY
    assert adm["properties"]["MainPID"] != str(res["pid"])
    raw = res["topology_raw"]
    assert raw["reason"] == TOPOLOGY
    assert len(raw["root_procs"]) == 2
    assert raw["supervisor_exists"] is False, "topology 不成立なら自己移動しない"
    assert raw["self_cgroup_after"] == raw["self_cgroup_before"]


def test_service_without_delegate_is_delegation_missing(disposable_units):
    res = disposable_units.run_helper("nodeleg", "admit_and_move", {"Delegate": None})
    assert res.get("error") is None, res
    adm = res["admission"]
    assert adm["ok"] is False
    assert adm["reason"] == DELEGATION
    # 判定は property から来る (cgroup 操作の成否からではない)
    assert adm["properties"]["Delegate"] == "no"
    assert adm["properties"]["MainPID"] == str(res["pid"])


# ------------------------------------------------------------------ 停止の層


@pytest.mark.parametrize("prop,value", [
    ("Type", "simple"),
    ("NotifyAccess", "main"),
    ("TimeoutStopSec", "infinity"),
    ("TimeoutStopSec", "0"),
    ("SendSIGKILL", "no"),
    ("FinalKillSignal", "SIGTERM"),
    ("TimeoutStopFailureMode", "abort"),
    ("KillMode", "mixed"),
])
def test_admission_rejects_disposable_units_with_unsupported_stop_policy(
        disposable_units, prop, value):
    case = f"stop-{prop.lower()}-{value.lower()}"
    res = disposable_units.run_helper(case, "admit_only", {prop: value})
    assert res.get("error") is None, res
    adm = res["admission"]
    assert adm["ok"] is False, adm
    assert adm["reason"] == STOP, adm


def _pid_alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return st.rsplit(")", 1)[1].split()[0] != "Z"


def _wait_file(path: Path, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text()
            if text.endswith("\n"):
                return text
        time.sleep(0.05)
    raise AssertionError(f"{path} が書かれなかった")


def test_exec_service_stop_kills_whole_cgroup_after_finite_timeout(disposable_units, tmp_path):
    pids_file = tmp_path / "pids"
    unit = disposable_units.start(
        "stopper", disposable_units.helper_command("stop_ignorer", pids_file),
        {"TimeoutStopSec": "2s"})
    main, child, grandchild = (int(x) for x in _wait_file(pids_file).split())
    assert disposable_units.show(unit, "MainPID") == str(main)
    time.sleep(0.5)  # main が自分を SIGSTOP するのを待つ
    assert Path(f"/proc/{main}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T"
    elapsed = disposable_units.stop(unit)
    assert 1.8 <= elapsed < 6, elapsed
    for pid in (main, child, grandchild):
        assert not _pid_alive(pid), pid
    assert disposable_units.listed() == []


def test_extend_timeout_request_does_not_lengthen_stop(disposable_units, tmp_path):
    report = tmp_path / "extend.json"
    unit = disposable_units.start(
        "extend", disposable_units.helper_command("extend_timeout", report),
        {"TimeoutStopSec": "2s"})
    first = json.loads(_wait_file(report))
    assert first["notify_socket_env"] is None, "NotifyAccess=none の main に NOTIFY_SOCKET は無い"
    assert first["sent_direct"] is not None
    elapsed = disposable_units.stop(unit)
    # EXTEND_TIMEOUT_USEC=60s を送っても TimeoutStopSec=2s + 余裕で返る
    assert 1.8 <= elapsed < 6, elapsed
    assert not _pid_alive(first["pid"])
    assert disposable_units.listed() == []

    res = disposable_units.run_helper("notifymain", "admit_only", {"NotifyAccess": "main"})
    assert res["admission"]["ok"] is False
    assert res["admission"]["reason"] == STOP


def test_real_systemctl_reaches_user_manager(systemd_environment):
    """固定 argv と sanitized env の実 systemctl が user manager に届く (存在しない unit でも
    systemd は既定値を返すので、11 key の形が実出力で成立することを確かめる)。"""
    raw = ua.run_show(ua.show_argv(ua.locate_systemctl(), "afxtest-absent-unit.service"),
                      ua.sanitized_env())
    props = ua.parse_show_output(raw)
    assert set(props) == set(ua.SHOW_PROPERTIES)
    assert props["MainPID"] == "0"
    with pytest.raises(ua.AdmissionError):
        ua.judge_properties(props, os.getpid())


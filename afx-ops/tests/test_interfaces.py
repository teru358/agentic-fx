"""interface 列挙の 1 実装 (`afx_ops.interfaces`) と `afx-ops interfaces` の契約。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from afx_ops import cli, interfaces
from afx_ops.interfaces import Candidate, InterfaceLookupError

SCRIPT = Path(sys.executable).parent / "afx-ops"


def _fake_ip(tmp_path: Path, body: str, name: str = "ip") -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _json_ip(tmp_path: Path, document: object, name: str = "ip") -> str:
    data = tmp_path / f"{name}.json"
    data.write_text(json.dumps(document), encoding="utf-8")
    return _fake_ip(tmp_path, f"cat '{data}'", name)


def _entry(ifindex: int, *locals_: str, **extra: object) -> dict:
    return {"ifindex": ifindex, "ifname": f"if{ifindex}",
            "addr_info": [{"family": "inet", "local": local, "prefixlen": 24, "scope": "global"}
                          for local in locals_], **extra}


def _list(path: str, **kwargs):
    return interfaces.list_interfaces(candidates=(path,), **kwargs)


def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def _no_children() -> bool:
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


# --- AC-25: 実行の形 ---------------------------------------------------------------

def test_fixed_absolute_candidates_in_the_documented_order():
    assert interfaces.IP_CANDIDATES == ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")
    assert interfaces.IP_ARGS == ("-j", "addr", "show", "up")
    assert interfaces.IP_TIMEOUT == 5.0 and interfaces.MAX_STDOUT == 65536


def test_the_first_existing_regular_file_is_used_and_path_is_not_consulted(tmp_path, monkeypatch):
    first = _json_ip(tmp_path, [_entry(2, "192.168.1.10")], "first")
    second = _json_ip(tmp_path, [_entry(3, "192.168.1.20")], "second")
    absent = str(tmp_path / "absent")
    monkeypatch.setenv("PATH", str(tmp_path))
    found = interfaces.list_interfaces(candidates=(absent, first, second))
    assert [c.address for c in found] == ["192.168.1.10"]


def test_no_candidate_is_a_lookup_failure(tmp_path):
    directory = tmp_path / "ip"
    directory.mkdir()
    with pytest.raises(InterfaceLookupError):
        interfaces.list_interfaces(candidates=(str(tmp_path / "none"), str(directory)))


def test_the_subprocess_is_spawned_with_the_documented_boundaries(tmp_path, monkeypatch):
    path = _json_ip(tmp_path, [_entry(2, "192.168.1.10")])
    seen = []
    real = subprocess.Popen

    def spy(argv, **kwargs):
        seen.append((argv, kwargs))
        return real(argv, **kwargs)

    monkeypatch.setattr(interfaces.subprocess, "Popen", spy)
    _list(path)
    (argv, kwargs), = seen
    assert argv == [path, "-j", "addr", "show", "up"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["cwd"] == "/"
    assert kwargs["env"] == {"LC_ALL": "C"}
    assert not kwargs.get("shell")
    assert kwargs["close_fds"] is True


def test_the_child_sees_a_clean_environment_and_cwd(tmp_path):
    record = tmp_path / "record"
    data = tmp_path / "data.json"
    data.write_text("[]")
    path = _fake_ip(tmp_path, f"pwd > '{record}'; env >> '{record}'; cat '{data}'")
    _list(path)
    lines = record.read_text().splitlines()
    assert lines[0] == "/"
    assert "LC_ALL=C" in lines
    assert not any(line.startswith(("HOME=", "XDG_")) for line in lines)


def test_stdout_is_read_once_per_call_and_ip_is_run_once(tmp_path):
    count = tmp_path / "count"
    data = tmp_path / "data.json"
    data.write_text("[]")
    path = _fake_ip(tmp_path, f"echo x >> '{count}'; cat '{data}'")
    _list(path)
    assert count.read_text().splitlines() == ["x"]


# --- AC-25: 失敗はすべて interface_lookup_failed -------------------------------------

@pytest.mark.parametrize("body", [
    "exit 1",
    "kill -9 $$",
    "printf '\\377\\376'",
    "printf 'not json'",
    "printf '{}'",
    "printf ''",
])
def test_failing_runs_are_lookup_failures_and_leave_no_child_or_fd(tmp_path, body):
    before = _fds()
    with pytest.raises(InterfaceLookupError):
        _list(_fake_ip(tmp_path, body))
    assert _fds() == before
    assert _no_children()


@pytest.mark.parametrize("document", [
    {"ifindex": 1},
    [1],
    [{"addr_info": []}],
    [{"ifindex": "2", "addr_info": []}],
    [{"ifindex": 0, "addr_info": []}],
    [{"ifindex": True, "addr_info": []}],
    [{"ifindex": 2}],
    [{"ifindex": 2, "addr_info": {}}],
    [{"ifindex": 2, "addr_info": [1]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "prefixlen": 24}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": "192.168.1.10"}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": 3, "prefixlen": 24}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": "192.168.1.10", "prefixlen": 33}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": "192.168.1.10", "prefixlen": -1}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": "192.168.1.10", "prefixlen": True}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": "192.168.001.10", "prefixlen": 24}]}],
    [{"ifindex": 2, "addr_info": [{"family": "inet", "local": "192.168.1.10/24", "prefixlen": 24}]}],
])
def test_schema_violations_are_lookup_failures_without_partial_results(tmp_path, document):
    # 先頭の要素は正しい。違反があれば部分結果を使わない
    good = _entry(1, "192.168.1.5")
    document = [good, *document] if isinstance(document, list) else document
    with pytest.raises(InterfaceLookupError):
        _list(_json_ip(tmp_path, document))


def test_unknown_keys_and_non_inet_families_are_ignored(tmp_path):
    document = [{"ifindex": 2, "ifname": "x", "future": {"k": [1]}, "addr_info": [
        {"family": "inet6", "local": "garbage", "prefixlen": "x"},
        {"local": "no-family"},
        {"family": "inet", "local": "192.168.1.10", "prefixlen": 24, "future_key": 1}]}]
    assert _list(_json_ip(tmp_path, document)) == (Candidate("192.168.1.10", 2, "global"),)


# --- AC-25: 候補の分類 ------------------------------------------------------------

def test_primary_and_secondary_addresses_on_one_interface_are_both_candidates(tmp_path):
    found = _list(_json_ip(tmp_path, [_entry(2, "192.168.1.10", "192.168.1.11")]))
    assert [c.address for c in found] == ["192.168.1.10", "192.168.1.11"]
    assert {c.ifindex for c in found} == {2}


def test_scope_comes_from_the_address_range_not_the_ip_scope_string(tmp_path):
    document = [{"ifindex": 2, "addr_info": [
        {"family": "inet", "local": "169.254.3.4", "prefixlen": 16, "scope": "global"},
        {"family": "inet", "local": "10.0.0.5", "prefixlen": 8, "scope": "link"}]}]
    found = _list(_json_ip(tmp_path, document))
    assert [(c.address, c.scope) for c in found] == [("10.0.0.5", "global"),
                                                     ("169.254.3.4", "link")]


def test_loopback_multicast_unspecified_and_broadcast_are_not_candidates(tmp_path):
    document = [_entry(1, "127.0.0.1", "127.9.9.9"),
                _entry(2, "224.0.0.1", "0.0.0.0", "255.255.255.255", "192.168.1.10")]
    assert [c.address for c in _list(_json_ip(tmp_path, document))] == ["192.168.1.10"]


def test_candidates_are_sorted_by_scope_ifindex_address_and_deduplicated(tmp_path):
    document = [_entry(9, "10.0.0.9", "169.254.9.9"), _entry(3, "10.0.0.9", "192.168.5.1"),
                _entry(5, "10.0.0.2", "169.254.1.1")]
    found = _list(_json_ip(tmp_path, document))
    assert [(c.address, c.ifindex, c.scope) for c in found] == [
        ("10.0.0.9", 3, "global"), ("192.168.5.1", 3, "global"), ("10.0.0.2", 5, "global"),
        ("169.254.1.1", 5, "link"), ("169.254.9.9", 9, "link")]


def test_addresses_on_one_interface_sort_numerically_not_as_text(tmp_path):
    found = _list(_json_ip(tmp_path, [_entry(2, "10.0.0.10", "10.0.0.9")]))
    assert [c.address for c in found] == ["10.0.0.9", "10.0.0.10"]


def test_mutation_secondary_dropped_is_caught():
    """M-T1-IFACE-SECONDARY: 最初の inet だけを候補にする変異は上の secondary 試験が落とす。"""
    document = json.dumps([_entry(2, "192.168.1.10", "192.168.1.11")]).encode()
    assert len(interfaces.parse_ip_json(document)) == 2


# --- AC-25: timeout と上限の境界 ----------------------------------------------------

class _Clock:
    """最初の呼出し(deadline の起点)は 0、以後は `now` を返す。"""

    def __init__(self, now: float) -> None:
        self.now, self.calls = now, 0

    def __call__(self) -> float:
        self.calls += 1
        return 0.0 if self.calls == 1 else self.now


@pytest.mark.parametrize(("now", "ok"), [(4.999, True), (5.000, False), (5.001, False)])
def test_timeout_boundary_at_five_seconds(tmp_path, monkeypatch, now, ok):
    path = _json_ip(tmp_path, [_entry(2, "192.168.1.10")])
    monkeypatch.setattr(interfaces.time, "monotonic", _Clock(now))
    if ok:
        assert _list(path)[0].address == "192.168.1.10"
    else:
        with pytest.raises(InterfaceLookupError):
            _list(path)
    assert _no_children()


def test_a_run_that_never_ends_is_killed_and_reaped(tmp_path):
    pid_file = tmp_path / "pid"
    path = _fake_ip(tmp_path, f"echo $$ > '{pid_file}'; exec sleep 30")
    before = _fds()
    with pytest.raises(InterfaceLookupError):
        _list(path, timeout=0.3)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert _fds() == before and _no_children()


def _padded(total: int) -> bytes:
    base = json.dumps([{"ifindex": 2, "addr_info": [], "pad": ""}]).encode()
    document = base.replace(b'"pad": ""', b'"pad": "' + b"a" * (total - len(base)) + b'"')
    assert len(document) == total
    return document


@pytest.mark.parametrize(("size", "ok"), [(65535, True), (65536, True), (65537, False)])
def test_stdout_limit_boundary(tmp_path, size, ok):
    data = tmp_path / "big.json"
    data.write_bytes(_padded(size))
    path = _fake_ip(tmp_path, f"cat '{data}'")
    before = _fds()
    if ok:
        assert _list(path) == ()
    else:
        with pytest.raises(InterfaceLookupError):
            _list(path)
    assert _fds() == before and _no_children()


def test_an_endless_producer_is_cut_at_the_limit_and_reaped(tmp_path):
    path = _fake_ip(tmp_path, "exec yes")
    with pytest.raises(InterfaceLookupError):
        _list(path)
    assert _no_children()


# --- afx-ops interfaces ---------------------------------------------------------

def test_the_command_prints_one_versioned_json_object(tmp_path, monkeypatch, capsys):
    document = [_entry(2, "192.168.1.10", "192.168.1.11")]
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (_json_ip(tmp_path, document),))
    assert cli.main(["interfaces"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out == ('{"interfaces":[{"address":"192.168.1.10","ifindex":2,"scope":"global"},'
                   '{"address":"192.168.1.11","ifindex":2,"scope":"global"}],"version":1}\n')


def test_the_command_failure_is_rc_71_with_one_fixed_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (_fake_ip(tmp_path, "exit 3"),))
    assert cli.main(["interfaces"]) == 71
    out, err = capsys.readouterr()
    assert out == "" and err == "afx-ops: interface_lookup_failed\n"


def test_the_command_does_not_touch_config_state_or_secret_dirs(tmp_path, monkeypatch, home):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (_json_ip(tmp_path, []),))
    assert cli.main(["interfaces"]) == 0
    assert list(home.iterdir()) == []


def test_the_real_command_and_the_package_agree(home):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    done = subprocess.run([str(SCRIPT), "interfaces"], capture_output=True, text=True,
                          timeout=30, env=env, cwd=home, stdin=subprocess.DEVNULL)
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout == interfaces.render_json(interfaces.list_interfaces())
    assert json.loads(done.stdout)["version"] == 1
    assert list(home.iterdir()) == []

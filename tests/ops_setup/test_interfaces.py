"""本体が `afx-ops interfaces` を有界 subprocess で呼ぶ契約。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.ops_setup import interfaces
from agentic_fx.ops_setup.feature_config import InterfaceLookupFailed
from agentic_fx.ops_setup.interfaces import Candidate, list_interfaces, parse

GOOD = {"interfaces": [{"address": "192.168.1.10", "ifindex": 2, "ifname": "enp6s0", "scope": "global"},
                       {"address": "192.168.1.11", "ifindex": 2, "ifname": "enp6s0", "scope": "global"},
                       {"address": "169.254.3.4", "ifindex": 3, "ifname": "wlp3s0", "scope": "link"}],
        "version": 2}


def _text(document) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


def _fake(tmp_path: Path, body: str, name: str = "afx-ops") -> str:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


def _emit(tmp_path: Path, text: str, *, rc: int = 0, stderr: str = "") -> str:
    payload = tmp_path / "payload"
    payload.write_bytes(text.encode())
    err = f"printf '%s' '{stderr}' >&2\n" if stderr else ""
    return _fake(tmp_path, f"cat {payload}\n{err}exit {rc}\n")


def test_a_valid_listing_is_returned_in_the_contract_order(tmp_path):
    found = list_interfaces(_emit(tmp_path, _text(GOOD)))
    assert found == (Candidate("192.168.1.10", 2, "global", "enp6s0"),
                     Candidate("192.168.1.11", 2, "global", "enp6s0"),
                     Candidate("169.254.3.4", 3, "link", "wlp3s0"))


def test_an_empty_listing_is_valid(tmp_path):
    assert list_interfaces(_emit(tmp_path, _text({"interfaces": [], "version": 2}))) == ()


def test_the_call_is_exactly_the_resolved_command_plus_interfaces_with_a_closed_stdin(tmp_path):
    log = tmp_path / "log"
    payload = tmp_path / "payload"
    payload.write_text(_text(GOOD))
    script = _fake(tmp_path, f'printf "%s\\n" "$#" "$1" > {log}; env >> {log}; '
                             f'if read -r line; then echo stdin-open >> {log}; fi; cat {payload}\n')
    list_interfaces(script)
    lines = log.read_text().splitlines()
    assert lines[:2] == ["1", "interfaces"]
    assert "stdin-open" not in lines
    assert [line for line in lines[2:] if "=" in line and not line.startswith(("PWD=", "SHLVL=", "_="))] == [
        f"PATH={os.defpath}"]


def test_a_python_module_command_is_split_into_words(tmp_path):
    payload = tmp_path / "payload"
    payload.write_text(_text(GOOD))
    runner = tmp_path / "runner.py"
    runner.write_text(f"import sys\nassert sys.argv[1:] == ['-m', 'afx_ops', 'interfaces'], sys.argv\n"
                      f"sys.stdout.write(open({str(payload)!r}).read())\n")
    # `-m afx_ops` の形は shell 同様に語へ分ける (実体は python)。
    wrapper = _fake(tmp_path, f'exec {sys.executable} {runner} "$@"\n', "wrap")
    assert list_interfaces(f"{wrapper} -m afx_ops")[0].address == "192.168.1.10"


@pytest.mark.parametrize("name", ["nonzero-rc", "stderr-output", "not-json", "empty"])
def test_failures_are_one_fixed_failure(tmp_path, name):
    command = {
        "nonzero-rc": lambda: _emit(tmp_path, _text(GOOD), rc=71),
        "stderr-output": lambda: _emit(tmp_path, _text(GOOD), stderr="afx-ops: interface_lookup_failed"),
        "not-json": lambda: _emit(tmp_path, "nope\n"),
        "empty": lambda: _emit(tmp_path, ""),
    }[name]()
    with pytest.raises(InterfaceLookupFailed):
        list_interfaces(command)


def test_a_missing_executable_is_a_lookup_failure(tmp_path):
    with pytest.raises(InterfaceLookupFailed):
        list_interfaces(str(tmp_path / "absent"))


def test_a_signal_death_is_a_lookup_failure(tmp_path):
    with pytest.raises(InterfaceLookupFailed):
        list_interfaces(_fake(tmp_path, "kill -9 $$\n"))


def test_the_timeout_is_ten_seconds_and_the_child_is_reaped(tmp_path):
    assert interfaces.TIMEOUT == 10.0
    pidfile = tmp_path / "pid"
    command = _fake(tmp_path, f"echo $$ > {pidfile}\nexec sleep 30\n")
    with pytest.raises(InterfaceLookupFailed):
        list_interfaces(command, timeout=0.4)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _padded(size: int) -> str:
    base = _text(GOOD)[:-1]
    return base + " " * (size - len(base) - 1) + "\n"


@pytest.mark.parametrize("size, ok", [(65535, True), (65536, True), (65537, False)])
def test_the_stdout_cap_is_65536_bytes(tmp_path, size, ok):
    assert interfaces.MAX_STDOUT == 65536
    command = _emit(tmp_path, _padded(size))
    if ok:
        assert len(list_interfaces(command)) == 3
    else:
        with pytest.raises(InterfaceLookupFailed):
            list_interfaces(command)


def test_an_endless_stdout_is_cut_and_the_child_reaped(tmp_path):
    pidfile = tmp_path / "pid"
    command = _fake(tmp_path, f"echo $$ > {pidfile}\nexec yes\n")
    with pytest.raises(InterfaceLookupFailed):
        list_interfaces(command, timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_no_pipe_fd_is_left_open_on_any_exit_path(tmp_path):
    before = set(os.listdir("/proc/self/fd"))
    good = _emit(tmp_path, _text(GOOD))
    list_interfaces(good)
    for command in (_emit(tmp_path, "x\n"), _emit(tmp_path, _text(GOOD), rc=3), str(tmp_path / "absent")):
        with pytest.raises(InterfaceLookupFailed):
            list_interfaces(command)
    assert set(os.listdir("/proc/self/fd")) == before


# --- 形式の検査 ---------------------------------------------------------------------------

def _doc(**override):
    document = json.loads(json.dumps(GOOD))
    document.update(override)
    return document


@pytest.mark.parametrize("document", [
    [], "x", 1, None,
    {"interfaces": [], "version": 2, "extra": 1},
    {"interfaces": []},
    {"version": 2},
    # 版ずれ: 旧 afx-ops の version 1 は受理しない
    _doc(version=1), _doc(version=3), _doc(version=True), _doc(version="2"), _doc(version=2.0),
    _doc(interfaces={}),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0"}]),
    # ifname は必須の空でない文字列
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "", "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": 3, "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": None, "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "global", "x": 1}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 0, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": True, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "host"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "link"}]),
    _doc(interfaces=[{"address": "169.254.3.4", "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "127.0.0.1", "ifindex": 1, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "0.0.0.0", "ifindex": 1, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "224.0.0.1", "ifindex": 1, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "255.255.255.255", "ifindex": 1, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.001.10", "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": " 192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "fe80::1", "ifindex": 2, "ifname": "eth0", "scope": "link"}]),
    _doc(interfaces=[{"address": 1, "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
    # 重複
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "global"},
                     {"address": "192.168.1.10", "ifindex": 3, "ifname": "eth0", "scope": "global"}]),
    # sort 違反: link が global より前 / ifindex 降順 / address 降順
    _doc(interfaces=[{"address": "169.254.3.4", "ifindex": 3, "ifname": "eth0", "scope": "link"},
                     {"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 3, "ifname": "eth0", "scope": "global"},
                     {"address": "192.168.1.20", "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
    _doc(interfaces=[{"address": "192.168.1.11", "ifindex": 2, "ifname": "eth0", "scope": "global"},
                     {"address": "192.168.1.10", "ifindex": 2, "ifname": "eth0", "scope": "global"}]),
])
def test_a_contract_violation_is_a_lookup_failure(document):
    with pytest.raises(InterfaceLookupFailed):
        parse(_text(document).encode())


@pytest.mark.parametrize("raw", [b"", b"{}", _text(GOOD).encode()[:-1], (_text(GOOD) + "\n").encode(),
                                 b"\xff\xfe\n", b"\xef\xbb\xbf" + _text(GOOD).encode()])
def test_a_framing_violation_is_a_lookup_failure(raw):
    with pytest.raises(InterfaceLookupFailed):
        parse(raw)


def test_the_body_never_imports_the_ops_package():
    code = ("import sys\n"
            "import agentic_fx.ops_setup.interfaces, agentic_fx.ops_setup.interactive, "
            "agentic_fx.ops_setup.reconfigure, agentic_fx.ops_setup.stages\n"
            "assert 'afx_ops' not in sys.modules, sorted(m for m in sys.modules if m.startswith('afx_ops'))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def test_no_body_source_names_the_ops_package_in_an_import():
    import ast
    root = Path(interfaces.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            offenders += [str(path) for name in names if name.split(".")[0] == "afx_ops"]
    assert offenders == []


def test_the_listing_carries_the_interface_name_of_each_candidate(tmp_path):
    found = list_interfaces(_emit(tmp_path, _text(GOOD)))
    assert [(c.address, c.ifname) for c in found] == [("192.168.1.10", "enp6s0"),
                                                      ("192.168.1.11", "enp6s0"),
                                                      ("169.254.3.4", "wlp3s0")]


def test_the_json_ifname_is_kept_as_is_and_only_the_display_escapes_it(tmp_path):
    document = _doc(interfaces=[{"address": "192.168.1.10", "ifindex": 2, "ifname": "en p6\x1b[31mé",
                                 "scope": "global"}])
    found = list_interfaces(_emit(tmp_path, _text(document)))
    assert found[0].ifname == "en p6\x1b[31mé"
    assert interfaces.display_name(found[0].ifname) == "en?p6??31m?"


@pytest.mark.parametrize(("raw", "shown"), [
    ("enp6s0", "enp6s0"), ("br-0.1_a", "br-0.1_a"), ("a b", "a?b"), ("a\nb", "a?b"),
    ("\x1b[2J", "??2J"), ("eé", "e?"), ("a/b", "a?b"), ("a:b", "a?b"), ("a$b", "a?b"),
])
def test_the_display_name_replaces_every_character_outside_the_safe_class(raw, shown):
    assert interfaces.display_name(raw) == shown

"""interface 列挙の 1 実装: `afx-ops interfaces`、probe、production の検査が同じ候補を使う。

本体のウィザードはこの command の出力だけを使うので、command の候補が probe と起動の受理する
address の集合と食い違わないことが、ウィザードだけが別の列挙を使う変異を封じる土台になる。
"""
from __future__ import annotations

import json
import socket

import pytest

from afx_ops import cli, interfaces, probe
from afx_ops.config import read_config
from afx_ops.isolation import startup

PRIMARY = "192.168.1.10"
SECONDARY = "192.168.1.11"
ABSENT = "192.168.77.5"


def _fake_ip(directory, *addresses: str) -> str:
    document = json.dumps([{"ifindex": 2, "addr_info": [
        {"family": "inet", "local": address, "prefixlen": 24} for address in addresses]}])
    script = directory / "ip"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{document}\nEOF\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


@pytest.fixture(autouse=True)
def _web_dirs(ops_dirs):
    dirs = ops_dirs("web")
    dirs.config.mkdir(parents=True, mode=0o700)
    dirs.config.chmod(0o700)
    return dirs


def _install(dirs, bind: str) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    path = dirs.config / "ops.toml"
    path.write_text('instance_root = "/srv/afx-probe"\nkey_dir = "/srv/afx-probe-keys"\n'
                    f'port = {port}\nbind = "{bind}"\nallow = ["10.0.0.0/8"]\n', encoding="utf-8")
    path.chmod(0o600)


def _production(dirs) -> str | None:
    import os
    config = read_config("web")
    try:
        plan = startup.StartupPlan.for_feature("web", web_port=config.port,
                                               web_bind=config.web_bind, web_auth=config.auth)
        startup.check_web_access(plan, config)
    except startup.StartupError as exc:
        return exc.reason
    finally:
        os.close(config.config_dir_fd)
    return None


def test_command_probe_and_production_accept_exactly_the_same_addresses(home, tmp_path, capsys,
                                                                       monkeypatch, _web_dirs):
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (_fake_ip(tmp_path, PRIMARY, SECONDARY),))
    assert cli.main(["interfaces"]) == 0
    listed = {item["address"] for item in json.loads(capsys.readouterr().out)["interfaces"]}
    assert listed == {PRIMARY, SECONDARY}
    for address in (PRIMARY, SECONDARY, ABSENT):
        _install(_web_dirs, address)
        production = _production(_web_dirs)
        rc = probe.main("web")
        err = capsys.readouterr().err
        if address in listed:
            assert production is None
            # 実 bind は本機に無い address で失敗するが、列挙の段は通っている
            assert (rc, err) in ((0, ""), (73, "afx-ops: probe_bind_failed\n"))
        else:
            assert production == "bind_not_local"
            assert (rc, err) == (71, "afx-ops: bind_not_local\n")


def test_every_entrance_runs_the_one_enumeration_function_once(home, tmp_path, capsys,
                                                               monkeypatch, _web_dirs):
    calls = []
    real = interfaces.list_interfaces
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (_fake_ip(tmp_path, PRIMARY),))
    monkeypatch.setattr(interfaces, "list_interfaces", lambda: calls.append(1) or real())
    cli.main(["interfaces"])
    capsys.readouterr()
    _install(_web_dirs, PRIMARY)
    _production(_web_dirs)
    probe.main("web")
    capsys.readouterr()
    assert calls == [1, 1, 1]

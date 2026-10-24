"""AC-12: G1〜G7 と C1〜C8 の全 fixture で、3 入口 (config・probe・production) が同じ reason を返す。

fixture は `_web_access_matrix` が仕様の 2 表から機械生成し (期待値は実装を呼ばずに書き下す)、
本体の reader の試験 (`tests/ops_setup/test_web_access_matrix.py`) も同じ JSON を読む。
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE / "isolation") not in sys.path:
    sys.path.insert(0, str(HERE / "isolation"))

import _web_access_matrix as matrix  # noqa: E402
from _afx_isolation_support import make_root, needs_landlock, needs_x86_64, run_canary  # noqa: E402
from afx_ops import interfaces, probe
from afx_ops.config import ConfigError, read_config
from afx_ops.isolation import startup

ROWS = matrix.rows()
ABSENT = "192.168.77.5"
OTHER = "192.168.1.99"


@pytest.fixture(scope="module")
def local_address() -> str:
    found = interfaces.list_interfaces()
    if not found:
        pytest.skip("この host に global / link の IPv4 が無い")
    assert ABSENT not in {c.address for c in found}
    return found[0].address


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_ip(directory: Path, row: dict, local: str) -> tuple[str, Path]:
    marker = directory / "ip-called"
    script = directory / "ip"
    state = row["interfaces"]
    if state == "failed":
        body = f"echo called >> '{marker}'\nexit 1"
    else:
        # 1 interface に primary と secondary の 2 address。本機の address は secondary 側に置く
        addresses = {"local": [OTHER, local], "absent": [OTHER], "na": [OTHER]}[state]
        document = json.dumps([{"ifindex": 2, "addr_info": [
            {"family": "inet", "local": address, "prefixlen": 24} for address in addresses]}])
        body = f"echo called >> '{marker}'\ncat <<'EOF'\n{document}\nEOF"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script), marker


def _build(dirs, row: dict, *, root: str, keys: str, port: int, local: str) -> None:
    """row が表す ops.toml・config dir・secret dir を XDG の領域に置く。"""
    if row.get("config_dir") == "missing":
        return
    dirs.config.mkdir(parents=True, mode=0o700)
    dirs.config.chmod(0o700)
    bind = {"loopback": "127.0.0.1", "local": local, "absent": ABSENT, "failed": ABSENT}[
        row.get("bind", "loopback")]
    text = (row["text"].replace("{ROOT}", root).replace("{KEYS}", keys)
            .replace("{PORT}", str(port)).replace("{BIND}", bind))
    path = dirs.config / "ops.toml"
    kind = row.get("ops_toml", "file")
    if kind in ("file", "other_uid"):
        path.write_bytes(bytes.fromhex(row["bytes_hex"]) if "bytes_hex" in row
                         else text.encode("utf-8"))
        path.chmod(row.get("mode", 0o600))
    elif kind == "symlink":
        target = dirs.config.parent / "elsewhere.toml"
        target.write_text(text, encoding="utf-8")
        target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    if row.get("config_dir") == "writable":
        dirs.config.chmod(0o777)
    secret = row.get("secret", "none")
    if secret != "none":
        dirs.secret.mkdir(mode=0o700)
        dirs.secret.chmod(0o700)
        if secret in ("safe", "unsafe"):
            password = dirs.secret / "web.password"
            password.write_bytes(b"A" * 22)
            password.chmod(0o600)
        if secret == "unsafe":
            (dirs.secret / "stray").write_text("x")


def _as_other_uid(monkeypatch, dirs, row: dict) -> None:
    if row.get("ops_toml") != "other_uid":
        return
    inode = (dirs.config / "ops.toml").stat().st_ino
    real = os.fstat

    def foreign(fd):
        st = real(fd)
        if stat.S_ISREG(st.st_mode) and st.st_ino == inode:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1,
                                   st.st_gid, st.st_size, int(st.st_atime), int(st.st_mtime),
                                   int(st.st_ctime)))
        return st

    monkeypatch.setattr(os, "fstat", foreign)


_BEFORE_ENUMERATION = frozenset({
    None, "config_dir_missing", "config_dir_invalid", "config_invalid", "allow_required",
    "unrestricted_not_supported", "auth_not_supported"})


def _check_called(row: dict, marker: Path) -> None:
    if row["interfaces"] == "na" or (row["expected"] in _BEFORE_ENUMERATION - {None}):
        assert not marker.exists(), "列挙に進まない入力で interface を列挙した"


def _ids(rows):
    return [row["id"] for row in rows]


# --- fixture の網羅 ---------------------------------------------------------------------

def test_the_checked_in_fixture_is_the_generated_one():
    assert matrix.FIXTURE.read_text(encoding="utf-8") == matrix.render()


def test_the_generator_covers_every_grammar_class_and_the_full_cross_product():
    ids = _ids(ROWS)
    assert len(ids) == len(set(ids))
    for prefix in ("G1", "G2", "G3-", "G3a", "G3b", "G4", "G5", "G6", "G7"):
        assert any(i.startswith(prefix) for i in ids), prefix
    c = [row for row in ROWS if row["group"] == "C"]
    assert len(c) == 4 * 3 * 2 * 4
    assert {(r["bind"], r["allow"], r["auth"], r["secret"]) for r in c} == {
        (b, a, u, s) for b in matrix.BINDS for a in matrix.ALLOWS for u in matrix.AUTHS
        for s in matrix.SECRETS}
    assert {row["expected"] for row in c} == {
        None, "allow_required", "unrestricted_not_supported", "auth_not_supported",
        "interface_lookup_failed", "bind_not_local", "secret_invalid", "secret_unexpected"}
    # 必須 fixture
    for needed in ("G5-bind-leading-space", "G7-auth-capital", "G3b-unknown-key",
                   "G4-port-bool", "G6-allow-ipv6", "G6-allow-empty-list",
                   "G6-allow-mixed-unrestricted", "G2-ops-toml-symlink", "G2-ops-toml-other-uid",
                   "G2-ops-toml-directory", "G2-ops-toml-mode-0644-accepted",
                   "G2-ops-toml-mode-0664", "G2-ops-toml-mode-0666", "G1-config-dir-missing",
                   "G3a-instance-root-missing", "G3a-key-dir-relative", "G3a-other-instance"):
        assert needed in ids, needed


def test_each_row_has_exactly_one_expected_final_result():
    for row in ROWS:
        assert row["expected"] is None or isinstance(row["expected"], str)
    # 表の先行行が後続行に勝つ: 無制限は secret や bind の状態によらず C2
    for row in ROWS:
        if row["group"] == "C" and row["allow"] == "unrestricted" and row["bind"] != "failed":
            expected = "unrestricted_not_supported"
            if row["bind"] in ("local", "absent"):
                assert row["expected"] == expected, row["id"]


# --- 入口 1: config (read_config + 起動と同じ検査) -----------------------------------------

def _config_reason(monkeypatch, row, local, tmp_path) -> str | None:
    script, marker = _fake_ip(tmp_path, row, local)
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (script,))
    try:
        config = read_config("web")
    except ConfigError as exc:
        _check_called(row, marker)
        return exc.reason
    try:
        plan = startup.StartupPlan.for_feature("web", web_port=config.port,
                                               web_bind=config.web_bind, web_auth=config.auth)
        startup.check_web_access(plan, config)
    except startup.StartupError as exc:
        return exc.reason
    finally:
        os.close(config.config_dir_fd)
        _check_called(row, marker)
    return None


@pytest.mark.parametrize("row", [r for r in ROWS if not r.get("body_only")], ids=_ids(
    [r for r in ROWS if not r.get("body_only")]))
def test_config_entry_reason(row, home, ops_dirs, monkeypatch, tmp_path, local_address):
    _build(ops_dirs("web"), row, root="/srv/afx-matrix", keys="/srv/afx-matrix-keys", port=_free_port(),
           local=local_address)
    _as_other_uid(monkeypatch, ops_dirs("web"), row)
    assert _config_reason(monkeypatch, row, local_address, tmp_path) == row["expected"]


# --- 入口 2: probe ------------------------------------------------------------------------

@pytest.mark.parametrize("row", [r for r in ROWS if not r.get("body_only")], ids=_ids(
    [r for r in ROWS if not r.get("body_only")]))
def test_probe_entry_reason(row, home, ops_dirs, monkeypatch, tmp_path, capsys, local_address):
    _build(ops_dirs("web"), row, root="/srv/afx-matrix", keys="/srv/afx-matrix-keys", port=_free_port(),
           local=local_address)
    _as_other_uid(monkeypatch, ops_dirs("web"), row)
    script, marker = _fake_ip(tmp_path, row, local_address)
    monkeypatch.setattr(interfaces, "IP_CANDIDATES", (script,))
    rc = probe.main("web")
    out, err = capsys.readouterr()
    assert out == ""
    _check_called(row, marker)
    if row["expected"] is None:
        assert (rc, err) == (0, "")
    else:
        assert (rc, err) == (71, f"afx-ops: {row['expected']}\n")


# --- 入口 3: production (実 process、隔離まで通す) -------------------------------------------

@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("row", [r for r in ROWS if not r.get("body_only")],
                         ids=_ids([r for r in ROWS if not r.get("body_only")]))
def test_production_entry_reason(row, home, ops_dirs, tmp_path, local_address):
    base = make_root(tmp_path / "iso")
    _build(ops_dirs("web"), row, root=str(base / "root"), keys=str(base / "keys"), port=_free_port(),
           local=local_address)
    script, marker = _fake_ip(tmp_path, row, local_address)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".state")}
    got = run_canary({"feature": "web", "root": str(base), "toml": True, "dirs": "xdg",
                      "op": "config_view", "params": {}, "fake_ip": script,
                      "foreign_owner": row.get("ops_toml") == "other_uid"}, env)
    _check_called(row, marker)
    if row["expected"] is None:
        assert got.rc == 0 and got.isolated, got.stderr[-1500:]
        assert got.stderr == ""
    else:
        assert got.rc == 71 and not got.isolated, got.stdout + got.stderr
        assert got.stderr == f"afx-ops: {row['expected']}\n"

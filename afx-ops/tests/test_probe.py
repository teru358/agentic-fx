"""``afx-ops probe <feature>``: 本体が subprocess で呼ぶ疎通確認の起動口。

隔離せず、鍵 dir を読まず、何も作らない。入力は config dir の ``ops.toml`` と機能の secret
file だけを自分で読み、戻りは終了コードと stderr の固定 reason 1 行だけ。
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from _oracle_harness import no_core
from afx_ops import cli, probe
from afx_ops.isolation.startup import feature_dirs

SCRIPT = Path(sys.executable).parent / "afx-ops"
MARK = "probe-mark-5d2c"
needs_strace = pytest.mark.skipif(shutil.which("strace") is None, reason="strace が無い")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install(home: Path, feature: str, *, port: int | None = None, extra: str = "") -> None:
    dirs = feature_dirs(feature)
    dirs.config.mkdir(parents=True, mode=0o700)
    dirs.config.chmod(0o700)
    text = (f'instance_root = "{home}/{MARK}-root"\n'
            f'key_dir = "{home}/{MARK}-keys-absent"\n')
    if feature == "web":
        text += f"port = {port if port is not None else _free_port()}\n"
    elif feature == "bot":
        text += ("guild_id = 123456789012345678\nchannel_id = 223456789012345678\n"
                 "allowlist_user_ids = []\nmention_user_ids = []\n")
    path = dirs.config / "ops.toml"
    path.write_text(text + extra, encoding="utf-8")
    path.chmod(0o600)
    dirs.secret.mkdir(mode=0o700)
    dirs.secret.chmod(0o700)
    for name in probe.SECRET_FILES.get(feature, ()):
        secret = dirs.secret / name
        secret.write_text(f"{MARK}-secret-{feature}\n")
        secret.chmod(0o600)


def _probe(args, env, home, *, prefix=(), stdin=subprocess.DEVNULL):
    return subprocess.run([*prefix, str(SCRIPT), *args], capture_output=True, text=True,
                          timeout=60, env=env, cwd=home, stdin=stdin, preexec_fn=no_core)


def _break(case: str, home: Path, feature: str) -> None:
    dirs = feature_dirs(feature)
    if case == "config_dir_missing":
        shutil.rmtree(dirs.config)
    elif case == "config_dir_invalid":
        dirs.config.chmod(0o777)
    elif case == "config_invalid":
        with (dirs.config / "ops.toml").open("a", encoding="utf-8") as f:
            f.write(f'{MARK.replace("-", "_")} = "{MARK}-value"\n')
    elif case == "secrets_failed":
        for name in probe.SECRET_FILES[feature]:
            (dirs.secret / name).unlink()


_CASES = {
    "ok_web": ("web", None, 0, None),
    # 試験の secret は bot token の形でないので、bot は送信せず入力の失敗で止まる
    "token_invalid_bot": ("bot", None, 71, "token_invalid"),
    # 試験の secret は URL でないので、notify は送信せず入力の失敗で止まる
    "url_invalid_notify": ("notify", None, 71, "url_invalid"),
    "config_dir_missing": ("bot", "config_dir_missing", 71, "config_dir_missing"),
    "config_dir_invalid": ("notify", "config_dir_invalid", 71, "config_dir_invalid"),
    "config_invalid": ("web", "config_invalid", 71, "config_invalid"),
    "secrets_failed": ("bot", "secrets_failed", 71, "secrets_failed"),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_probe_return_contract(case, child_env, home):
    feature, fault, rc, reason = _CASES[case]
    _install(home, feature)
    if fault:
        _break(fault, home, feature)
    done = _probe(["probe", feature], child_env, home)
    assert done.returncode == rc
    assert done.stdout == ""
    assert done.stderr == ("" if reason is None else f"afx-ops: {reason}\n")


@pytest.mark.parametrize("args", [["probe"], ["probe", "nope"], ["probe", "bot", "extra"],
                                  ["probe", "probe"]])
def test_probe_with_wrong_arguments_is_unknown_feature(args, child_env, home):
    done = _probe(args, child_env, home)
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("afx-ops: unknown_feature")
    assert list(home.iterdir()) == []


def test_probe_vocabulary_is_fixed():
    assert (probe.RC_OK, cli.RC_UNKNOWN_FEATURE, probe.RC_NOT_IMPLEMENTED,
            probe.RC_RUNTIME_UNAVAILABLE, probe.RC_INPUT_FAILED, probe.RC_PROBE_FAILED) == \
        (0, 2, 69, 70, 71, 73)
    assert set(probe.PROBES) == set(cli.FEATURES)
    with pytest.raises(TypeError):
        probe.PROBES["bot"] = probe.PROBES["web"]  # type: ignore[index]


def test_read_inputs_hides_and_closes_the_config_directory_fd(child_env, home):
    _install(home, "notify")
    old_env = os.environ.copy()
    os.environ.clear()
    os.environ.update(child_env)
    try:
        config, _secrets = probe._read_inputs("notify")
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert config.config_dir_fd is None


def test_probe_reads_its_own_inputs_and_nothing_from_argv_env_or_stdin(child_env, home):
    _install(home, "notify")
    read_end, write_end = os.pipe()
    os.write(write_end, f"{MARK}-stdin\n".encode())
    os.close(write_end)
    try:
        done = _probe(["probe", "notify"], child_env, home, stdin=read_end)
        assert (done.returncode, done.stderr) == (71, "afx-ops: url_invalid\n")
        # stdin は 1 byte も消費していない
        assert os.read(read_end, 4096) == f"{MARK}-stdin\n".encode()
    finally:
        os.close(read_end)
    # secret を消すと止まる (= secret file を自分で読んでいる)
    _break("secrets_failed", home, "notify")
    done = _probe(["probe", "notify"], child_env, home)
    assert done.returncode == 71
    assert done.stderr == "afx-ops: secrets_failed\n"


@pytest.mark.parametrize("case", sorted(_CASES))
def test_probe_output_never_contains_secret_path_or_config_values(case, child_env, home):
    feature, fault, _rc, _reason = _CASES[case]
    _install(home, feature)
    if fault:
        _break(fault, home, feature)
    done = _probe(["probe", feature], child_env, home)
    out = done.stdout + done.stderr
    assert MARK not in out and MARK.replace("-", "_") not in out
    assert str(home) not in out


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_probe_creates_nothing_and_needs_neither_unit_nor_key_dir(feature, child_env, home):
    _install(home, feature)
    before = sorted((str(p), p.stat().st_mtime_ns) for p in home.rglob("*"))
    # 通常の子 process (NoNewPrivs なし、unit の外)。key_dir は存在しない
    done = _probe(["probe", feature], child_env, home)
    assert done.returncode == {"bot": 71, "web": 0, "notify": 71}[feature], done.stderr
    assert sorted((str(p), p.stat().st_mtime_ns) for p in home.rglob("*")) == before
    assert not (home / ".local").exists()


def test_web_probe_binds_loopback_and_releases_the_port(child_env, home):
    port = _free_port()
    _install(home, "web", port=port)
    done = _probe(["probe", "web"], child_env, home)
    assert done.returncode == 0, done.stderr
    # probe は bind した port を閉じて終わる
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
    for host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as busy:
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy.bind((host, port))
            busy.listen(1)
            done = _probe(["probe", "web"], child_env, home)
        assert done.returncode == 73, (host, done.stderr)
        assert done.stderr == "afx-ops: probe_bind_failed\n"


def test_web_probe_refuses_port_zero(child_env, home):
    _install(home, "web", port=0)
    done = _probe(["probe", "web"], child_env, home)
    assert done.returncode == 71
    assert done.stderr == "afx-ops: config_invalid\n"


def _trace(tmp_path, feature, calls, child_env, home):
    trace = tmp_path / f"{feature}.trace"
    done = _probe(["probe", feature], child_env, home,
                  prefix=("strace", "-f", "-qq", "-e", f"trace={calls}", "-o", str(trace)))
    return done, trace.read_text().splitlines()


@needs_strace
def test_web_probe_never_accepts(tmp_path, child_env, home):
    _install(home, "web")
    done, lines = _trace(tmp_path, "web", "listen,accept,accept4", child_env, home)
    assert done.returncode == 0, done.stderr
    assert [line for line in lines if re.search(r"\baccept4?\(", line)] == []
    # 観測が空振りでないこと: listen は trace に現れる
    assert any(re.search(r"\blisten\(", line) for line in lines)


@needs_strace
@pytest.mark.parametrize("feature", ["bot", "notify"])
def test_bot_and_notify_probes_make_no_network_calls(feature, tmp_path, child_env, home):
    _install(home, feature)
    done, lines = _trace(tmp_path, feature, "socket,connect,openat", child_env, home)
    # notify は URL でない secret、bot は token の形でない secret を入力の失敗として止める
    # (名前解決にも進まない)
    assert done.returncode == {"bot": 71, "notify": 71}[feature], done.stderr
    assert [line for line in lines if "AF_INET" in line] == []
    assert [line for line in lines if re.search(r"\bconnect\(", line)] == []
    # 観測が空振りでないこと: secret file は開いている
    assert any(probe.SECRET_FILES[feature][0] in line for line in lines)


def test_web_probe_releases_the_listener_it_bound(monkeypatch):
    from afx_ops.config import OpsConfig
    closed = []

    class _Listener:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(probe, "prebind_listener", lambda port: _Listener())
    probe.probe_web(OpsConfig(instance_root=Path("/srv/afx"), key_dir=Path("/srv/keys"),
                              config_dir_fd=None, port=18080), {})
    assert closed == [True]


def test_probe_input_reading_leaves_no_descriptor_open(home):
    config_dir = feature_dirs("web").config
    config_dir.mkdir(parents=True, mode=0o700)
    ops_toml = config_dir / "ops.toml"
    ops_toml.write_text('instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\nport = 18080\n',
                        encoding="utf-8")
    ops_toml.chmod(0o600)
    before = set(os.listdir("/proc/self/fd"))
    probe._read_inputs("web")
    assert set(os.listdir("/proc/self/fd")) == before

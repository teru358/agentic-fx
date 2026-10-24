"""web の設定作業 (入力 file・password・前処理・要約・status 行) の単体試験。"""
from __future__ import annotations

import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.ops_setup.feature_config import web_access_reason, write_inputs
from agentic_fx.ops_setup.web_setup import (
    SecretPlan,
    WebOptions,
    WebSetupError,
    access_line,
    apply_secret,
    apply_target,
    check_password,
    cleanup_temporaries,
    exposure,
    generate_password,
    generated_password_line,
    options_problem,
    plan_for_input,
    read_config_file,
    read_password_file,
    summary_line,
    target_from_config_file,
)
from web_support import LAN

PW = "A" * 22
TMP_PASSWORD = ".web.password.{}.tmp"
TMP_CONFIG = ".ops.toml.{}.tmp"
HEX = "0123456789abcdef" * 2


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    return tmp_path / "c" / "agentic-fx" / "ops" / "web"


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "instance"
    path.mkdir()
    return path


def _file(path: Path, data: bytes | str, mode: int) -> str:
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    return str(path)


# --- option の許容組み合わせ -----------------------------------------------------

_O = WebOptions
ENABLE_OK = [
    _O(), _O(ask=True),
    _O(non_interactive=True, config_file="c", secret_action="keep"),
    _O(non_interactive=True, config_file="c", secret_action="remove"),
    _O(non_interactive=True, config_file="c", secret_action="absent"),
    _O(non_interactive=True, config_file="c", secret_action="install", password_file="p"),
]
ENABLE_BAD = [
    _O(ask=True, non_interactive=True, config_file="c", secret_action="keep"),
    _O(ask=True, config_file="c"), _O(ask=True, password_file="p"), _O(ask=True, secret_action="keep"),
    _O(config_file="c"), _O(password_file="p"), _O(secret_action="keep"),
    _O(non_interactive=True), _O(non_interactive=True, config_file="c"),
    _O(non_interactive=True, secret_action="keep"),
    _O(non_interactive=True, config_file="c", secret_action="bogus"),
    _O(non_interactive=True, config_file="c", secret_action="install"),
    _O(non_interactive=True, config_file="c", secret_action="keep", password_file="p"),
    _O(non_interactive=True, config_file="c", secret_action="remove", password_file="p"),
    _O(non_interactive=True, config_file="c", secret_action="absent", password_file="p"),
    _O(duplicated=True),
]


@pytest.mark.parametrize("options", ENABLE_OK)
def test_allowed_enable_option_combinations(options):
    assert options_problem(options, reconfigure=False) is None


@pytest.mark.parametrize("options", ENABLE_BAD)
def test_every_other_enable_option_combination_is_input_required(options):
    assert options_problem(options, reconfigure=False) == "setup_input_required"


def test_reconfigure_takes_the_same_inputs_but_never_ask():
    assert options_problem(_O(), reconfigure=True) is None
    assert options_problem(_O(non_interactive=True, config_file="c", secret_action="absent"),
                           reconfigure=True) is None
    assert options_problem(_O(ask=True), reconfigure=True) == "setup_input_required"
    for options in ENABLE_BAD:
        assert options_problem(options, reconfigure=True) == "setup_input_required"


# --- --config-file ---------------------------------------------------------------

@pytest.mark.parametrize("mode, ok", [(0o644, True), (0o600, True), (0o664, False), (0o666, False),
                                      (0o660, False), (0o620, False), (0o602, False)])
def test_config_file_mode_boundary(tmp_path, mode, ok):
    path = _file(tmp_path / "t.toml", "port = 8788\n", mode)
    if ok:
        assert read_config_file(path) == {"port": 8788}
    else:
        with pytest.raises(WebSetupError) as raised:
            read_config_file(path)
        assert (raised.value.reason, raised.value.rc) == ("setup_input_invalid", 2)


@pytest.mark.parametrize("size, ok", [(0, False), (1, True), (65536, True), (65537, False)])
def test_config_file_size_boundary(tmp_path, size, ok):
    body = b"#" * (size - 1) + b"\n" if size else b""
    path = _file(tmp_path / "t.toml", body, 0o600)
    if ok:
        read_config_file(path)
    else:
        with pytest.raises(WebSetupError):
            read_config_file(path)


def test_config_file_symlink_directory_fifo_and_missing_are_refused(tmp_path):
    real = _file(tmp_path / "real.toml", "port = 1\n", 0o600)
    (tmp_path / "link.toml").symlink_to(real)
    (tmp_path / "dir.toml").mkdir()
    os.mkfifo(tmp_path / "fifo.toml")
    for name in ("link.toml", "dir.toml", "fifo.toml", "absent.toml"):
        with pytest.raises(WebSetupError) as raised:
            read_config_file(str(tmp_path / name))
        assert raised.value.reason == "setup_input_invalid"
        assert str(tmp_path) not in str(raised.value)


def test_config_file_other_owner_is_refused(tmp_path, monkeypatch):
    path = _file(tmp_path / "t.toml", "port = 1\n", 0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(WebSetupError):
        read_config_file(path)


@pytest.mark.parametrize("body", [b"\xff\xfe", b"port = = 1\n", b"[port]\n"])
def test_config_file_encoding_and_toml_errors_are_input_invalid(tmp_path, body):
    path = _file(tmp_path / "t.toml", body, 0o600)
    try:
        value = read_config_file(path)
    except WebSetupError as error:
        assert error.reason == "setup_input_invalid"
    else:
        with pytest.raises(WebSetupError):
            target_from_config_file(value)


def test_config_file_swapped_after_open_is_read_from_the_opened_file(tmp_path):
    path = tmp_path / "t.toml"
    _file(path, "port = 8788\n", 0o600)

    def swap(_name):
        other = tmp_path / "other.toml"
        _file(other, "port = 9999\n", 0o600)
        os.replace(other, path)

    assert read_config_file(str(path), after_open=swap) == {"port": 8788}


def test_config_file_that_grows_while_being_read_is_refused(tmp_path):
    path = tmp_path / "t.toml"
    _file(path, "port = 8788\n", 0o600)

    def grow(_name):
        with path.open("ab") as handle:
            handle.write(b"# more\n")

    with pytest.raises(WebSetupError):
        read_config_file(str(path), after_open=grow)


@pytest.mark.parametrize("parsed", [
    {"port": 8788, "instance_root": "/x"}, {"port": 8788, "key_dir": "/x"}, {"port": 8788, "unknown": 1},
    {}, {"port": 0}, {"port": 8788, "bind": LAN}, {"port": 8788, "bind": "localhost"},
    {"port": 8788, "auth": "Basic"}, {"port": 8788, "allow": []},
    {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8", "0.0.0.0/0"]},
    {"port": 8788, "auth": "basic", "user": "Afx"}, {"port": 8788, "auth": "basic", "user": "afx:u"},
    {"port": 8788, "auth": "basic", "user": "afx\n"}, {"port": 8788, "auth": "basic", "user": ""},
    {"port": 8788, "auth": "basic", "user": "a" * 33}, {"port": 8788, "auth": "basic", "user": 1},
])
def test_target_from_config_file_rejects_what_is_not_a_valid_target(parsed):
    with pytest.raises(WebSetupError) as raised:
        target_from_config_file(parsed)
    assert (raised.value.reason, raised.value.rc) == ("setup_input_invalid", 2)


@pytest.mark.parametrize("parsed", [
    {"port": 8788}, {"port": 8788, "bind": "127.0.0.1"},
    {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "basic"},
    {"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"]},
    {"port": 8788, "allow": ["0.0.0.0/0"], "auth": "basic"},
    {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "basic", "user": "ops-web"},
    {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "basic", "user": "a" * 32},
])
def test_target_from_config_file_accepts_valid_targets(parsed):
    assert target_from_config_file(parsed) == parsed


# --- --password-file ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, ok", [
    (b"A" * 7, False), (b"A" * 8, True), (b"A" * 32, True), (b"A" * 33, False),
    (b"A" * 8 + b"\n", True), (b"A" * 32 + b"\n", True), (b"A" * 33 + b"\n", False),
    (b"A" * 7 + b"\n", False), (b"A" * 8 + b"\r\n", False), (b"A" * 8 + b"\n\n", False),
    (b"A" * 32 + b"\r\n", False), (b"A" * 32 + b"\n\n", False),
    (b"AAAAAAAA\nAAAAAAAA", False), (b"AAAAAAAA AAAAAAAA", False), (b"AAAAAAAA\tAAAAAAAA", False),
    (b"AAAAAAAA:AAAAAAAA", True), ("パスワードパスワードパスワード".encode(), False),
    (b"A" * 16, True), (b"A" * 128, False),
    (b"A" * 8 + b"\x7f", False), (b"A" * 8 + b"\x00", False),
])
def test_password_file_grammar_boundary(tmp_path, raw, ok):
    path = _file(tmp_path / "p", raw, 0o600)
    if ok:
        assert read_password_file(path) == raw.rstrip(b"\n").decode()
    else:
        with pytest.raises(WebSetupError) as raised:
            read_password_file(path)
        assert raised.value.reason == "secret_invalid"


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644, 0o660, 0o664, 0o666, 0o700, 0o620])
def test_password_file_mode_must_be_exactly_0600(tmp_path, mode):
    path = _file(tmp_path / "p", PW, mode)
    with pytest.raises(WebSetupError) as raised:
        read_password_file(path)
    assert raised.value.reason == "secret_invalid"


def test_password_file_symlink_other_owner_and_nonregular_are_refused(tmp_path, monkeypatch):
    real = _file(tmp_path / "real", PW, 0o600)
    (tmp_path / "link").symlink_to(real)
    (tmp_path / "dir").mkdir()
    for name in ("link", "dir", "absent"):
        with pytest.raises(WebSetupError):
            read_password_file(str(tmp_path / name))
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(WebSetupError):
        read_password_file(real)


def test_password_file_swapped_or_changed_during_the_read_is_not_adopted(tmp_path):
    path = tmp_path / "p"
    _file(path, PW, 0o600)

    def swap(_name):
        other = tmp_path / "q"
        _file(other, "B" * 22, 0o600)
        os.replace(other, path)

    assert read_password_file(str(path), after_open=swap) == PW

    def change_mode(_name):
        path.chmod(0o644)

    with pytest.raises(WebSetupError):
        read_password_file(str(path), after_open=change_mode)


def test_password_diagnostics_never_contain_the_value(tmp_path):
    path = _file(tmp_path / "p", "secret-value-123456789", 0o644)
    with pytest.raises(WebSetupError) as raised:
        read_password_file(path)
    assert "secret-value" not in repr(raised.value) and "secret-value" not in str(raised.value)


# --- password の文法と生成 ---------------------------------------------------------------------

def test_generated_password_is_unpadded_base64url_of_16_random_bytes():
    value = generate_password(lambda n: bytes(range(n)))
    assert len(value) == 22 and "=" not in value
    assert base64.urlsafe_b64decode(value + "==") == bytes(range(16))
    assert check_password(value) == value


def test_generated_passwords_use_the_csprng_and_differ():
    assert generate_password() != generate_password()


@pytest.mark.parametrize("value, ok", [("A" * 7, False), ("A" * 8, True), ("A" * 32, True),
                                       ("A" * 33, False), ("a:b" * 6, True), ("a b" * 6, False),
                                       ("é" * 8, False), ("A" * 7 + "\n", False), ("A" * 8 + "\n", False),
                                       ("", False), ("A" * 16, True), ("A" * 128, False)])
def test_password_grammar(value, ok):
    if ok:
        assert check_password(value) == value
    else:
        with pytest.raises(WebSetupError):
            check_password(value)


def test_secret_plan_never_shows_the_password_in_its_repr():
    assert PW not in repr(SecretPlan("install", PW))


# --- 非対話の secret action の表 (§5.2) ----------------------------------------------------------

BASIC = {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"}
NONE = {"port": 8788}


@pytest.mark.parametrize("values, live, action, password, expected", [
    (BASIC, "safe", "keep", None, "keep"),
    (BASIC, "absent", "install", PW, "install"),
    (BASIC, "safe", "install", PW, "install"),
    (NONE, "safe", "remove", None, "remove"),
    (NONE, "absent", "absent", None, "absent"),
])
def test_secret_action_table_accepts(values, live, action, password, expected):
    assert plan_for_input(values, live, action, password).action == expected


@pytest.mark.parametrize("values, live, action, password", [
    (BASIC, "absent", "keep", None), (BASIC, "safe", "remove", None), (BASIC, "safe", "absent", None),
    (BASIC, "absent", "absent", None), (BASIC, "absent", "remove", None),
    (NONE, "safe", "keep", None), (NONE, "safe", "absent", None), (NONE, "safe", "install", PW),
    (NONE, "absent", "install", PW), (NONE, "absent", "keep", None), (NONE, "absent", "remove", None),
    (BASIC, "safe", "keep", PW), (NONE, "safe", "remove", PW), (NONE, "absent", "absent", PW),
    (BASIC, "safe", "install", None),
])
def test_secret_action_table_rejects_the_rest_as_input_required(values, live, action, password):
    with pytest.raises(WebSetupError) as raised:
        plan_for_input(values, live, action, password)
    assert (raised.value.reason, raised.value.rc) == ("setup_input_required", 2)


@pytest.mark.parametrize("action", ["keep", "install", "remove", "absent"])
@pytest.mark.parametrize("values", [BASIC, NONE])
def test_an_unsafe_live_secret_stops_whatever_the_action(values, action):
    with pytest.raises(WebSetupError) as raised:
        plan_for_input(values, "unsafe", action, PW if action == "install" else None)
    assert raised.value.reason == "secret_invalid"


# --- 前処理 (0): 一時 file の除去 ----------------------------------------------------------------

def _prepare(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    return xdg / "secret", xdg / "config"


def test_cleanup_removes_only_exact_writer_temporaries_of_this_user(root, xdg):
    secret, config = _prepare(root, xdg)
    gone = [secret / TMP_PASSWORD.format(HEX), config / TMP_CONFIG.format(HEX)]
    for path in gone:
        path.write_text("x")
    keep = [secret / "web.password", secret / ".web.password.not-a-temporary", config / "ops.toml",
            config / ".ops.toml.tmp",
            config / TMP_CONFIG.format(HEX[:31]), config / (TMP_CONFIG.format(HEX) + "x"),
            config / TMP_CONFIG.format(HEX.upper())]
    (secret / "web.password").write_text(PW)
    (secret / "web.password").chmod(0o600)
    for path in keep[1:2] + keep[3:]:
        path.write_text("x")
    cleanup_temporaries(root)
    assert not any(path.exists() for path in gone)
    assert all(path.exists() for path in keep)


def test_cleanup_syncs_each_directory_it_changed(root, xdg, monkeypatch):
    secret, config = _prepare(root, xdg)
    (secret / TMP_PASSWORD.format(HEX)).write_text("x")
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.readlink(f"/proc/self/fd/{fd}")), real_fsync(fd))[1])
    cleanup_temporaries(root)
    assert synced == [str(secret)]


def test_cleanup_without_directories_is_a_no_op(root, xdg):
    cleanup_temporaries(root)
    assert not xdg.exists()


@pytest.mark.parametrize("where, make, reason", [
    ("secret", "symlink", "secret_invalid"),
    ("secret", "directory", "secret_invalid"),
    ("secret", "fifo", "secret_invalid"),
    ("config", "symlink", "config_dir_invalid"),
    ("config", "directory", "config_dir_invalid"),
])
def test_cleanup_stops_on_a_same_named_non_regular_entry_and_removes_nothing(root, xdg, where, make, reason):
    secret, config = _prepare(root, xdg)
    directory = secret if where == "secret" else config
    good = directory / (TMP_PASSWORD if where == "secret" else TMP_CONFIG).format("a" * 32)
    good.write_text("x")
    bad = directory / (TMP_PASSWORD if where == "secret" else TMP_CONFIG).format("b" * 32)
    if make == "symlink":
        bad.symlink_to("/nonexistent")
    elif make == "directory":
        bad.mkdir()
    else:
        os.mkfifo(bad)
    with pytest.raises(WebSetupError) as raised:
        cleanup_temporaries(root)
    assert raised.value.reason == reason
    assert good.exists()


def test_cleanup_stops_on_another_users_temporary(root, xdg, monkeypatch):
    secret, _config = _prepare(root, xdg)
    (secret / TMP_PASSWORD.format(HEX)).write_text("x")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(WebSetupError) as raised:
        cleanup_temporaries(root)
    assert raised.value.reason == "secret_invalid"


def test_cleanup_leaves_no_fd_open(root, xdg):
    secret, _config = _prepare(root, xdg)
    (secret / TMP_PASSWORD.format(HEX)).write_text("x")
    before = set(os.listdir("/proc/self/fd"))
    cleanup_temporaries(root)
    assert set(os.listdir("/proc/self/fd")) == before


# --- 前処理 (a) (b): 反映と順序 --------------------------------------------------------------------

def test_install_writes_a_0600_password_through_the_atomic_steps(root, xdg):
    events: list[str] = []
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"},
                 SecretPlan("install", PW), hook=events.append)
    target = xdg / "secret" / "web.password"
    assert target.read_text() == PW and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert events == ["secret_tmp_created", "secret_tmp_synced",
                      "secret_renamed", "secret_dir_synced", "secret_applied", "config_tmp_created",
                      "config_tmp_synced", "config_renamed", "config_dir_synced", "config_applied"]
    assert not [p for p in (xdg / "secret").iterdir() if p.name.endswith(".tmp")]


def test_generated_password_is_shown_once_after_the_directory_sync_and_before_the_config(root, xdg):
    events: list[str] = []
    shown: list[str] = []

    def show(value, user):
        events.append("shown")
        shown.append((user, value))

    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"},
                 SecretPlan("install", PW, generated=True), hook=events.append, show_password=show)
    assert shown == [("afx", PW)]
    assert events.index("secret_dir_synced") < events.index("shown") < events.index("config_tmp_created")


def test_a_typed_password_is_never_shown(root, xdg):
    shown: list[str] = []
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"},
                 SecretPlan("install", PW), show_password=lambda *args: shown.append(args))
    assert shown == []


def test_the_shown_user_is_the_configured_user(root, xdg):
    shown: list[tuple[str, str]] = []
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "ops-web"},
                 SecretPlan("install", PW, generated=True), show_password=lambda pw, user: shown.append((user, pw)))
    assert shown == [("ops-web", PW)]


def test_the_generated_password_line_names_the_user_and_the_password_once():
    assert generated_password_line("ops-web", PW) == (
        f"ユーザー名: ops-web / 生成した password (この表示は 1 回だけです): {PW}")


def test_remove_unlinks_then_syncs_the_directory(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"},
                 SecretPlan("install", PW))
    events: list[str] = []
    apply_target(root, {"port": 8788}, SecretPlan("remove"), hook=events.append)
    assert not (xdg / "secret" / "web.password").exists()
    assert events.index("secret_unlinked") < events.index("secret_dir_synced") < events.index("config_tmp_created")


@pytest.mark.parametrize("action", ["keep", "absent"])
def test_keep_and_absent_touch_no_secret(root, xdg, action):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"},
                 SecretPlan("install", PW))
    before = (xdg / "secret").stat().st_mtime_ns, (xdg / "secret" / "web.password").read_bytes()
    events: list[str] = []
    apply_secret(root, SecretPlan(action), hook=events.append)
    assert events == []
    assert ((xdg / "secret").stat().st_mtime_ns, (xdg / "secret" / "web.password").read_bytes()) == before


def test_an_identical_config_is_not_rewritten(root, xdg):
    values = {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "none"}
    apply_target(root, values, SecretPlan("absent"))
    events: list[str] = []
    apply_target(root, values, SecretPlan("absent"), hook=events.append)
    assert "config_renamed" not in events


def test_install_replaces_a_safe_existing_password_atomically(root, xdg):
    values = {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"}
    apply_target(root, values, SecretPlan("install", PW))
    apply_target(root, values, SecretPlan("install", "B" * 22))
    assert (xdg / "secret" / "web.password").read_text() == "B" * 22


def test_an_invalid_password_is_rejected_before_anything_is_written(root, xdg):
    with pytest.raises(WebSetupError):
        apply_secret(root, SecretPlan("install", "short"))
    assert not (xdg / "secret").exists()


def test_an_unsafe_secret_directory_is_secret_invalid(root, xdg):
    _prepare(root, xdg)
    (xdg / "secret").chmod(0o755)
    with pytest.raises(WebSetupError) as raised:
        apply_secret(root, SecretPlan("install", PW))
    assert raised.value.reason == "secret_invalid"


def test_apply_target_leaves_no_fd_open_on_failure(root, xdg):
    _prepare(root, xdg)
    (xdg / "config" / "ops.toml").chmod(0o666)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(WebSetupError) as raised:
        apply_target(root, {"port": 9000}, SecretPlan("absent"))
    assert raised.value.reason == "config_dir_invalid"
    assert set(os.listdir("/proc/self/fd")) == before


# --- 要約と status 行 -------------------------------------------------------------------------------

@pytest.mark.parametrize("values, text", [
    ({"port": 8788}, "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 none"),
    ({"port": 8788, "auth": "basic"}, "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 basic (afx)"),
    ({"port": 8788, "auth": "basic", "user": "ops-web"},
     "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 basic (ops-web)"),
    ({"port": 8788, "auth": "none", "user": "ops-web"}, "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 none"),
    ({"port": 9000, "bind": "127.0.0.1", "allow": ["0.0.0.0/0"], "auth": "none"},
     "web: 待受 127.0.0.1:9000 / 公開 自機のみ / 認証 none"),
    ({"port": 9000, "bind": "127.0.0.2", "allow": ["10.0.0.0/8"], "auth": "basic"},
     "web: 待受 127.0.0.2:9000 / 公開 自機のみ / 認証 basic (afx)"),
    ({"port": 8788, "bind": LAN, "allow": ["10.0.0.1", "192.168.1.0/24", "172.16.0.0/12"], "auth": "basic"},
     f"web: 待受 {LAN}:8788 / 公開 許可範囲 3 件 / 認証 basic (afx)"),
    ({"port": 8788, "bind": LAN, "allow": ["10.0.0.1"], "auth": "none"},
     f"web: 待受 {LAN}:8788 / 公開 許可範囲 1 件 / 認証 none"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "none"},
     f"web: 待受 {LAN}:8788 / 公開 到達できる全 IPv4 / 認証 none"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "basic"},
     f"web: 待受 {LAN}:8788 / 公開 到達できる全 IPv4 / 認証 basic (afx)"),
])
def test_summary_line_forms(values, text):
    assert summary_line(values) == text


def test_summary_never_carries_an_allow_element():
    line = summary_line({"port": 8788, "bind": LAN, "allow": ["203.0.113.77", "198.51.100.0/24"], "auth": "basic"})
    assert "203.0.113" not in line and "198.51.100" not in line and "web.password" not in line


def test_exposure_words():
    assert exposure({"port": 1}) == "自機のみ"


def _stage(operation="enable", stage="enabled"):
    return {"operation": operation, "stage": stage}


def test_access_line_is_not_configured_without_calling_the_reader(root, xdg):
    def forbidden():
        raise AssertionError("reader called")

    for saved in (None, _stage("disable", "disabled")):
        assert access_line(root, saved, interfaces=forbidden) == "web-access: unavailable reason=not_configured"


@pytest.mark.parametrize("values, lister, line", [
    ({"port": 8788}, None, "web-access: bind=127.0.0.1:8788 allow=loopback-default auth=none"),
    ({"port": 8788, "bind": "127.0.0.1", "allow": ["10.0.0.0/8"]}, None,
     "web-access: bind=127.0.0.1:8788 allow=restricted auth=none"),
    ({"port": 8788, "bind": "127.0.0.1", "allow": ["0.0.0.0/0"]}, None,
     "web-access: bind=127.0.0.1:8788 allow=unrestricted auth=none"),
    ({"port": 8788, "bind": LAN, "allow": ["203.0.113.5", "10.0.0.0/8"], "auth": "none"}, [LAN],
     f"web-access: bind={LAN}:8788 allow=restricted auth=none"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "none"}, [LAN],
     f"web-access: bind={LAN}:8788 allow=unrestricted auth=none"),
])
def test_access_line_for_a_healthy_enabled_web(root, xdg, values, lister, line):
    write_inputs(root, "web", values)
    got = access_line(root, _stage(), interfaces=(lambda: lister) if lister is not None else None)
    assert got == line
    assert "203.0.113" not in got and "10.0.0" not in got


def test_access_line_basic_with_a_password_never_shows_the_password(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"},
                 SecretPlan("install", PW))
    got = access_line(root, _stage(), interfaces=lambda: [LAN])
    assert got == f"web-access: bind={LAN}:8788 allow=restricted auth=basic user=afx"
    assert PW not in got


def test_access_line_basic_shows_the_configured_user_and_the_default(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "basic",
                        "user": "ops-web"}, SecretPlan("install", PW))
    assert access_line(root, _stage(), interfaces=lambda: [LAN]) == \
        f"web-access: bind={LAN}:8788 allow=restricted auth=basic user=ops-web"
    text = (xdg / "config" / "ops.toml").read_text().replace('user = "ops-web"\n', "")
    (xdg / "config" / "ops.toml").write_text(text)
    assert access_line(root, _stage(), interfaces=lambda: [LAN]) == \
        f"web-access: bind={LAN}:8788 allow=restricted auth=basic user=afx"


def test_access_line_none_never_shows_a_user(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "none", "user": "ops"},
                 SecretPlan("absent"))
    assert "user" not in access_line(root, _stage(), interfaces=lambda: [LAN])


def test_access_line_unavailable_reasons_follow_the_shared_reader(root, xdg):
    assert access_line(root, _stage()) == "web-access: unavailable reason=config_dir_missing"
    write_inputs(root, "web", {"port": 8788})
    (xdg / "config" / "ops.toml").write_text("not = [toml")
    assert access_line(root, _stage()) == "web-access: unavailable reason=config_invalid"
    (xdg / "config" / "ops.toml").chmod(0o666)
    assert access_line(root, _stage()) == "web-access: unavailable reason=config_dir_invalid"
    (xdg / "config" / "ops.toml").unlink()
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"]})
    assert access_line(root, _stage(), interfaces=lambda: [LAN]) == f"web-access: bind={LAN}:8788 allow=restricted auth=none"
    assert access_line(root, _stage(), interfaces=lambda: ["192.168.9.9"]) == \
        "web-access: unavailable reason=bind_not_local"
    assert access_line(root, _stage()) == "web-access: unavailable reason=interface_lookup_failed"
    write_inputs(root, "web", {"port": 8788, "auth": "basic"})
    assert access_line(root, _stage()) == "web-access: unavailable reason=secret_missing"


def test_access_line_unavailable_never_leaks_values_or_paths(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["203.0.113.5"]})
    (xdg / "secret" / "stray").write_text("x")
    got = access_line(root, _stage(), interfaces=lambda: [LAN])
    assert got == "web-access: unavailable reason=secret_invalid"
    assert str(root) not in got and "203.0.113" not in got and LAN not in got and "8788" not in got


def test_status_line_uses_the_same_reason_as_the_reader_for_every_state(root, xdg):
    for values in ({"port": 8788}, {"port": 8788, "auth": "basic"}):
        write_inputs(root, "web", values)
        reason = web_access_reason(root)
        line = access_line(root, _stage())
        assert line.endswith(f"reason={reason}") if reason else line.startswith("web-access: bind=")


def test_the_subprocess_import_does_not_pull_in_the_ops_package():
    code = "import sys, agentic_fx.ops_setup.web_setup, agentic_fx.ops_setup.web_wizard; assert 'afx_ops' not in sys.modules"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr

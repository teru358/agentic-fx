from __future__ import annotations

import os
import stat
from pathlib import Path

from agentic_fx.ops_setup.feature_config import collect, write_inputs


def test_bot_write_creates_checked_files_with_expected_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    root = tmp_path / "instance"
    values = {
        "guild_id": 10**16,
        "channel_id": 10**16 + 1,
        "allowlist_user_ids": [10**16 + 2],
        "mention_user_ids": [10**16 + 2],
        "token": "secret-token",
    }

    write_inputs(root, "bot", values)
    found = collect(root, "bot")
    base = tmp_path / "config-home" / "agentic-fx" / "ops" / "bot"

    assert found.complete
    assert found.values["guild_id"] == 10**16
    assert "secret-token" not in (base / "config" / "ops.toml").read_text()
    assert stat.S_IMODE(os.stat(base).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(base / "config").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(base / "secret").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(base / "config" / "ops.toml").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(base / "secret" / "discord.token").st_mode) == 0o600


def test_matching_existing_inputs_are_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    root = tmp_path / "instance"
    values = {"port": 8788}
    write_inputs(root, "web", values)
    config = tmp_path / "config-home" / "agentic-fx" / "ops" / "web" / "config" / "ops.toml"
    before = (config.read_bytes(), config.stat().st_mtime_ns)

    write_inputs(root, "web", values)

    assert (config.read_bytes(), config.stat().st_mtime_ns) == before


import pytest

from agentic_fx.ops_setup.feature_config import InputError, redact, validate

BOT = {"guild_id": 10**16, "channel_id": 10**16 + 1, "allowlist_user_ids": [10**16 + 2], "token": "t"}


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    return tmp_path / "c" / "agentic-fx" / "ops"


def test_collect_reports_exact_missing_fields(tmp_path, xdg):
    assert collect(tmp_path, "bot").missing == ("guild_id", "channel_id", "allowlist_user_ids", "mention_user_ids", "token")
    assert collect(tmp_path, "web").missing == ("port",)
    assert collect(tmp_path, "notify").missing == ("webhook_url",)


def test_collect_never_returns_secret_content(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    assert collect(tmp_path, "bot").values["token"] == "present"
    write_inputs(tmp_path, "notify", {"webhook_url": "https://h.example.com/p/SECRET"})
    assert collect(tmp_path, "notify").values == {"webhook_url": "present"}


def test_collect_rejects_secret_with_read_only_mode(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    secret = xdg / "bot" / "secret" / "discord.token"
    secret.chmod(0o400)

    assert not collect(tmp_path, "bot").complete


@pytest.mark.parametrize("mutate", [
    lambda base: (base / "secret" / "discord.token").chmod(0o640),
    lambda base: (base / "secret").chmod(0o750),
    lambda base: (base / "config" / "ops.toml").chmod(0o666),
    lambda base: (base / "config").chmod(0o777),
    lambda base: base.chmod(0o755),
])
def test_collect_treats_unsafe_modes_as_missing(tmp_path, xdg, mutate):
    write_inputs(tmp_path, "bot", BOT)
    base = xdg / "bot"
    mutate(base)
    assert not collect(tmp_path, "bot").complete


def test_collect_rejects_config_for_other_instance_or_garbage(tmp_path, xdg):
    write_inputs(tmp_path / "a", "web", {"port": 8788})
    assert not collect(tmp_path / "b", "web").complete
    (xdg / "web" / "config" / "ops.toml").write_text("not = [toml")
    assert not collect(tmp_path / "a", "web").complete


def test_bot_mention_defaults_to_allowlist_and_role_is_optional(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT | {"mention_role_id": 10**16 + 9})
    text = (xdg / "bot" / "config" / "ops.toml").read_text()
    assert f"mention_user_ids = [{10**16 + 2}]" in text
    assert f"mention_role_id = {10**16 + 9}" in text
    assert "token" not in text


@pytest.mark.parametrize("feature,values", [
    ("web", {"port": 0}), ("web", {"port": 65536}), ("web", {"port": True}), ("web", {"port": "80"}),
    ("notify", {"webhook_url": "http://h.example.com/x"}),
    ("notify", {"webhook_url": "https://u:p@h.example.com/x"}),
    ("notify", {"webhook_url": "https://h.example.com:8443/x"}),
    ("notify", {"webhook_url": "https://h.example.com/x#f"}),
    ("notify", {"webhook_url": ""}),
    ("bot", BOT | {"guild_id": 10**16 - 1}),
    ("bot", BOT | {"channel_id": 10**20}),
    ("bot", BOT | {"allowlist_user_ids": [10**16, 10**16]}),
    ("bot", BOT | {"token": ""}),
])
def test_validate_rejects(feature, values):
    with pytest.raises(InputError):
        validate(feature, values)


def test_validate_accepts_port_bounds():
    assert validate("web", {"port": 1}) == {"port": 1}
    assert validate("web", {"port": 65535}) == {"port": 65535}


def test_write_leaves_no_temporary_files_and_rewrites_on_change(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    write_inputs(tmp_path, "web", {"port": 8789})
    assert collect(tmp_path, "web").values == {"port": 8789}
    assert sorted(p.name for p in (xdg / "web").rglob("*") if p.is_file()) == ["ops.toml"]


def test_write_rejects_a_secret_directory_symlink(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    base = xdg / "notify"
    (base / "config").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (base / "secret").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        write_inputs(tmp_path, "notify", {"webhook_url": "https://h.example.com/x"})
    assert not list(target.iterdir())


def test_write_rejects_parent_directory_symlink(tmp_path, xdg):
    ops = xdg
    ops.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    ops.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        write_inputs(tmp_path, "web", {"port": 8788})

    assert not list(target.iterdir())


def test_collect_accepts_hardened_config_modes_without_prompting(tmp_path, xdg):
    write_inputs(tmp_path, "web", {"port": 8788})
    base = xdg / "web"
    (base / "config").chmod(0o700)
    (base / "config" / "ops.toml").chmod(0o600)
    assert collect(tmp_path, "web").complete


def test_write_normalizes_new_modes_despite_umask(tmp_path, xdg):
    old = os.umask(0o077)
    try:
        write_inputs(tmp_path, "web", {"port": 8788})
    finally:
        os.umask(old)
    base = xdg / "web"
    assert stat.S_IMODE((base / "config").stat().st_mode) == 0o755
    assert stat.S_IMODE((base / "config" / "ops.toml").stat().st_mode) == 0o644


def test_atomic_write_retries_short_writes(tmp_path, xdg, monkeypatch):
    import agentic_fx.ops_setup.feature_config as feature_config

    original_write = feature_config.os.write

    def short_write(fd, data):
        return original_write(fd, data[:1])

    monkeypatch.setattr(feature_config.os, "write", short_write)
    write_inputs(tmp_path, "web", {"port": 8788})
    assert "port = 8788\n" in (xdg / "web" / "config" / "ops.toml").read_text()


def test_redact_hides_secret_and_url_path():
    assert redact("bot", "tok") == "<redacted>"
    assert redact("notify", "https://hooks.example.com/api/SECRET") == "hooks.example.com"

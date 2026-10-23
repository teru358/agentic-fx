from __future__ import annotations

import os
import stat
from pathlib import Path

from agentic_fx.ops_setup.feature_config import collect, unsafe_reason, write_inputs


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


def _hand_edit(xdg, feature: str, extra: str) -> Path:
    path = xdg / feature / "config" / "ops.toml"
    path.write_text(path.read_text() + extra)
    return path


def test_trade_keys_added_by_hand_are_kept_when_the_inputs_are_collected_and_rewritten(
        tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    path = _hand_edit(xdg, "bot", 'trade_notices = "results"\ntrade_amounts = "relative"\n')

    found = collect(tmp_path, "bot")

    assert found.complete
    assert (found.values["trade_notices"], found.values["trade_amounts"]) == ("results",
                                                                              "relative")
    write_inputs(tmp_path, "bot", found.values | {"allowlist_user_ids": [10**16 + 3],
                                                   "mention_user_ids": [10**16 + 3]})
    text = path.read_text()
    assert 'trade_notices = "results"' in text and 'trade_amounts = "relative"' in text
    assert f"allowlist_user_ids = [{10**16 + 3}]" in text


def test_notify_trade_key_is_written_as_a_toml_boolean_and_survives_a_rewrite(tmp_path, xdg):
    write_inputs(tmp_path, "notify", {"webhook_url": "https://h.example.com/x"})
    path = _hand_edit(xdg, "notify", "deliver_trade_events = true\n")

    found = collect(tmp_path, "notify")
    assert found.values["deliver_trade_events"] is True
    write_inputs(tmp_path, "notify", found.values | {"webhook_url": "https://h.example.com/y"})

    assert "deliver_trade_events = true\n" in path.read_text()
    write_inputs(tmp_path, "notify", {"webhook_url": "https://h.example.com/y",
                                       "deliver_trade_events": False})
    assert "deliver_trade_events = false\n" in path.read_text()


def test_old_inputs_without_the_trade_keys_are_complete_and_stay_without_them(tmp_path, xdg):
    write_inputs(tmp_path, "bot", BOT)
    write_inputs(tmp_path, "notify", {"webhook_url": "https://h.example.com/x"})

    for feature in ("bot", "notify"):
        found = collect(tmp_path, feature)
        assert found.complete
        assert not {"trade_notices", "trade_amounts", "deliver_trade_events"} & set(found.values)
    assert "trade_" not in (xdg / "bot" / "config" / "ops.toml").read_text()
    assert "trade_" not in (xdg / "notify" / "config" / "ops.toml").read_text()


@pytest.mark.parametrize("feature,extra", [
    ("bot", 'trade_notices = "everything"\n'), ("bot", "trade_notices = true\n"),
    ("bot", 'trade_amounts = "abs"\n'), ("bot", "deliver_trade_events = true\n"),
    ("notify", 'deliver_trade_events = "yes"\n'), ("notify", "deliver_trade_events = 1\n"),
    ("notify", 'trade_notices = "all"\n'),
])
def test_invalid_trade_keys_in_an_existing_config_make_the_inputs_unusable(
        tmp_path, xdg, feature, extra):
    write_inputs(tmp_path, feature, BOT if feature == "bot"
                 else {"webhook_url": "https://h.example.com/x"})
    _hand_edit(xdg, feature, extra)

    assert unsafe_reason(tmp_path, feature) == "config_file"


@pytest.mark.parametrize("feature,values", [
    ("bot", BOT | {"trade_notices": "everything"}), ("bot", BOT | {"trade_notices": 1}),
    ("bot", BOT | {"trade_amounts": "abs"}), ("bot", BOT | {"trade_amounts": True}),
    ("notify", {"webhook_url": "https://h.example.com/x", "deliver_trade_events": 1}),
    ("notify", {"webhook_url": "https://h.example.com/x", "deliver_trade_events": "true"}),
])
def test_validate_rejects_invalid_trade_values(feature, values):
    with pytest.raises(InputError):
        validate(feature, values)


def test_the_accepted_trade_notice_values_match_afx_ops():
    import ast

    from agentic_fx.ops_setup.feature_config import TRADE_AMOUNTS, TRADE_NOTICES

    root = Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops"
    tree = ast.parse((root / "trade_events.py").read_text(encoding="utf-8"))
    notices = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and node.target.id == "NOTICE_KINDS")
    keys = tuple(k.value for k in notices.args[0].keys)
    assert TRADE_NOTICES == keys
    config = ast.parse((root / "config.py").read_text(encoding="utf-8"))
    amounts = next(node.value for node in config.body if isinstance(node, ast.Assign)
                   and getattr(node.targets[0], "id", None) == "TRADE_AMOUNTS")
    assert TRADE_AMOUNTS == tuple(e.value for e in amounts.elts)


def test_bot_ops_notices_survives_a_writer_round_trip_and_rejects_other_values(tmp_path, xdg):
    values = BOT | {"ops_notices": "all"}
    write_inputs(tmp_path, "bot", values)

    assert collect(tmp_path, "bot").values["ops_notices"] == "all"
    write_inputs(tmp_path, "bot", collect(tmp_path, "bot").values | {"token": "t"})
    assert 'ops_notices = "all"' in (xdg / "bot" / "config" / "ops.toml").read_text()
    for invalid in ("everything", "ALL", 1, True, None):
        with pytest.raises(InputError):
            validate("bot", BOT | {"ops_notices": invalid})

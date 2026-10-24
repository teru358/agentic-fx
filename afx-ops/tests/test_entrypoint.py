"""console script `afx-ops <feature>` の起動口。"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from afx_ops import cli
from afx_ops.isolation.startup import feature_dirs

SCRIPT = Path(sys.executable).parent / "afx-ops"


def _run(args, env, home):
    done = subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                          env=env, cwd=home)
    return done


def _write_config(home, feature):
    path = feature_dirs(feature).config / "ops.toml"
    path.parent.mkdir(parents=True, mode=0o700)
    path.parent.chmod(0o700)
    text = 'instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\n'
    if feature == "web":
        text += "port = 8080\n"
    elif feature == "bot":
        text += ("guild_id = 12345678901234567\nchannel_id = 12345678901234567\n"
                 "allowlist_user_ids = []\nmention_role_id = 12345678901234567\n"
                 "mention_user_ids = []\n")
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_unknown_feature_exits_nonzero_with_fixed_reason(child_env, home):
    for args in (["nope"], [], ["bot", "extra"]):
        done = _run(args, child_env, home)
        assert done.returncode == 2
        assert "unknown_feature" in done.stderr
        assert done.stdout == ""
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_known_feature_outside_the_unit_stops_at_the_isolation_check(child_env, home, feature):
    # 起動口は設定を隔離前に読み、固定の隔離順へ進む。
    _write_config(home, feature)
    done = _run([feature], child_env, home)
    assert done.returncode == 71
    assert done.stderr.strip().splitlines() == ["afx-ops: unit_no_new_privs"]
    assert done.stdout == ""
    assert not (home / ".local").exists()


def test_module_entry_gives_the_same_result(child_env, home):
    _write_config(home, "web")
    done = subprocess.run([sys.executable, "-I", "-m", "afx_ops", "web"], capture_output=True,
                          text=True, timeout=30, env=child_env, cwd=home)
    assert done.returncode == 71
    assert done.stderr.strip().splitlines() == ["afx-ops: unit_no_new_privs"]
    assert not (home / ".local").exists()


def test_config_error_with_an_unprintable_key_stays_on_one_stderr_line(child_env, home):
    path = _write_config(home, "notify")
    with path.open("a", encoding="utf-8") as f:
        f.write('"unprintable\\nkey" = "ignored"\n')

    done = _run(["notify"], child_env, home)

    assert done.returncode == 71
    assert done.stderr.splitlines() == ["afx-ops: config_invalid (<invalid>)"]
    assert done.stdout == ""


def test_cli_enters_through_the_fixed_plan():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert {"for_feature", "run"} <= attrs
    assert "_for_tests" not in attrs


def test_cli_closes_the_config_dir_fd_only_before_startup_owns_it():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    closes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
              and node.func.attr == "close"]
    assert len(closes) == 1


def test_runtime_table_is_complete_and_immutable():
    assert set(cli.RUNTIMES) == {"bot", "web", "notify"}
    assert set(cli.PRELOADS) == {"bot", "web", "notify"}
    with pytest.raises(TypeError):
        cli.RUNTIMES["bot"] = cli.RUNTIMES["web"]  # type: ignore[index]
    with pytest.raises(TypeError):
        cli.PRELOADS["bot"] = cli.PRELOADS["web"]  # type: ignore[index]


def test_cli_has_no_runtime_replacement_hook():
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    assert _runtime_replacements(tree, "RUNTIMES") == []
    assert _runtime_replacements(tree, "PRELOADS") == []


def _runtime_replacements(tree, table):
    found = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for target in targets:
            if isinstance(target, ast.Name) and target.id == table:
                initial_table = (isinstance(node, ast.AnnAssign)
                                 and isinstance(node.value, ast.Call)
                                 and isinstance(node.value.func, ast.Name)
                                 and node.value.func.id == "MappingProxyType")
                if not initial_table:
                    found.append(table)
            elif (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                  and target.value.id == table):
                found.append(f"{table}[]")
            elif (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Call)
                  and isinstance(target.value.func, ast.Name)
                  and target.value.func.id == "globals"):
                found.append("globals")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == table):
            found.append("setattr")
    return found


@pytest.mark.parametrize(("source", "expected"), [
    ("RUNTIMES = replacement\n", "RUNTIMES"),
    ("RUNTIMES['web'] = replacement\n", "RUNTIMES[]"),
    ("globals()['RUNTIMES'] = replacement\n", "globals"),
    ("setattr(cli, 'RUNTIMES', replacement)\n", "setattr"),
    ("del RUNTIMES\n", "RUNTIMES"),
])
def test_runtime_pin_rejects_each_replacement_form(source, expected):
    assert _runtime_replacements(ast.parse(source), "RUNTIMES") == [expected]


@pytest.mark.parametrize(("source", "expected"), [
    ("PRELOADS = replacement\n", "PRELOADS"),
    ("PRELOADS['web'] = replacement\n", "PRELOADS[]"),
    ("globals()['PRELOADS'] = replacement\n", "globals"),
    ("setattr(cli, 'PRELOADS', replacement)\n", "setattr"),
    ("del PRELOADS\n", "PRELOADS"),
])
def test_preload_pin_rejects_each_replacement_form(source, expected):
    assert _runtime_replacements(ast.parse(source), "PRELOADS") == [expected]


def test_runtime_import_failure_has_its_own_fixed_exit_status(tmp_path, capsys):
    from afx_ops.config import OpsConfig
    from afx_ops.isolation import startup

    def failed_import():
        raise ImportError("not available")

    rc = startup.run(object(), OpsConfig(tmp_path, tmp_path, None), lambda isolated: 0,
                     preload=failed_import)

    assert rc == 70
    assert capsys.readouterr().err == "afx-ops: runtime_import_failed\n"


def test_runtime_import_failure_closes_the_config_fd(tmp_path):
    from afx_ops.config import OpsConfig
    from afx_ops.isolation import startup

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    rc = startup.run(object(), OpsConfig(tmp_path, tmp_path, fd), lambda isolated: 0,
                     preload=lambda: (_ for _ in ()).throw(ImportError()))

    assert rc == 70
    with pytest.raises(OSError):
        os.fstat(fd)


def test_runtime_base_exception_closes_the_config_fd(tmp_path):
    from afx_ops.config import OpsConfig
    from afx_ops.isolation import startup

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    with pytest.raises(KeyboardInterrupt):
        startup.run(object(), OpsConfig(tmp_path, tmp_path, fd), lambda isolated: 0,
                    preload=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(OSError):
        os.fstat(fd)


def test_config_dir_owner_does_not_close_a_reused_fd(tmp_path):
    from afx_ops.isolation import startup

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    owner = startup._ConfigDirOwner(fd)
    owner.close()
    reused = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        assert reused == fd
        owner.close()
        os.fstat(reused)
    finally:
        os.close(reused)


def test_cli_closes_config_when_plan_construction_fails(monkeypatch, tmp_path):
    from afx_ops.config import OpsConfig

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    config = OpsConfig(tmp_path, tmp_path, fd)
    monkeypatch.setattr(cli, "read_config", lambda _feature: config)
    monkeypatch.setattr(cli.startup.StartupPlan, "for_feature",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))

    assert cli.main(["notify"]) == 71
    with pytest.raises(OSError):
        os.fstat(fd)


def test_web_startup_plan_carries_the_configured_port(home, monkeypatch):
    config_dir = feature_dirs("web").config
    config_dir.mkdir(parents=True, mode=0o700)
    ops_toml = config_dir / "ops.toml"
    ops_toml.write_text('instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\nport = 18080\n',
                        encoding="utf-8")
    ops_toml.chmod(0o600)
    seen = []
    monkeypatch.setattr(cli.startup, "run", lambda plan, config, body, **kwargs: seen.append(plan) or 0)
    assert cli.main(["web"]) == 0
    assert seen[0].web_port == 18080


def test_notify_bot_and_web_runtimes_are_wired():
    from afx_ops import bot
    from afx_ops.notify import runtime as notify_runtime
    from afx_ops.web import runtime as web_runtime
    assert cli.RUNTIMES["notify"] is notify_runtime.run
    assert cli.RUNTIMES["web"] is web_runtime.run
    assert cli.RUNTIMES["bot"] is bot.run


def test_bot_entry_hands_the_isolated_object_to_the_runtime(monkeypatch):
    from afx_ops import bot
    from afx_ops.bot import runtime as bot_runtime
    seen = []
    monkeypatch.setattr(bot_runtime, "run", lambda isolated: seen.append(isolated) or 7)
    marker = object()
    assert bot.run(marker) == 7
    assert seen == [marker]


def test_bot_preload_imports_discord_and_the_bot_runtime_before_isolation():
    # 隔離後の業務処理が新しい module を読まないよう、preload が runtime module まで読む
    done = subprocess.run([sys.executable, "-I", "-c",
                           "import sys, afx_ops.cli as c; c.PRELOADS['bot'](); "
                           "print('discord' in sys.modules, 'afx_ops.bot.runtime' in sys.modules)"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["True", "True"]


def test_importing_the_entrypoint_leaves_aiohttp_to_the_preload():
    # aiohttp の import は起動順の preload (隔離前、失敗は runtime_import_failed) が行う
    done = subprocess.run([sys.executable, "-I", "-c",
                           "import sys, afx_ops.cli; print('aiohttp' in sys.modules)"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"


def _web_config(home):
    config_dir = feature_dirs("web").config
    config_dir.mkdir(parents=True, mode=0o700)
    ops_toml = config_dir / "ops.toml"
    ops_toml.write_text('instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\nport = 18080\n',
                        encoding="utf-8")
    ops_toml.chmod(0o600)


def test_web_startup_is_given_the_web_preload(home, monkeypatch):
    _web_config(home)
    seen = []
    monkeypatch.setattr(cli.startup, "run",
                        lambda plan, config, body, **kwargs: seen.append(kwargs["preload"]) or 0)
    assert cli.main(["web"]) == 0
    assert seen == [cli.PRELOADS["web"]]


def test_probe_web_fails_when_the_configured_port_is_taken():
    import socket
    from pathlib import Path

    from afx_ops import probe
    from afx_ops.config import OpsConfig

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        with pytest.raises(probe.ProbeError) as raised:
            probe.probe_web(OpsConfig(instance_root=Path("/srv/afx"), key_dir=Path("/srv/keys"),
                                      config_dir_fd=None, port=holder.getsockname()[1]), {})
        assert raised.value.reason == "probe_bind_failed"
    finally:
        holder.close()


@pytest.mark.parametrize(("feature", "extra", "expected"), [
    ("bot", 'trade_notices = "fills_closes"\ntrade_amounts = "relative"\n',
     {"trade_notices": "fills_closes", "trade_amounts": "relative"}),
    ("bot", 'ops_notices = "all"\n', {"ops_notices": "all"}),
    ("notify", "deliver_trade_events = true\n", {"deliver_trade_events": True}),
])
def test_ac24_trade_settings_are_read_before_isolation_and_handed_to_the_run(
        feature, extra, expected, home, monkeypatch):
    path = _write_config(home, feature)
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    seen = []
    monkeypatch.setattr(cli.startup, "run",
                        lambda plan, config, body, **kwargs: seen.append(config) or 0)

    assert cli.main([feature]) == 0

    try:
        for key, value in expected.items():
            assert getattr(seen[0], key) == value
    finally:
        os.close(seen[0].config_dir_fd)


def test_ac24_isolated_runtimes_do_not_read_the_config_again():
    import inspect

    from afx_ops.bot import adapter, runtime as bot_runtime
    from afx_ops.notify import runtime as notify_runtime

    for module in (adapter, bot_runtime, notify_runtime):
        source = inspect.getsource(module)
        assert "read_config" not in source and "ops.toml" not in source

from __future__ import annotations

import hashlib
import errno
import os
import pty
import select
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]


def _run(command: list[str], *, cwd: Path, env: dict[str, str], input_text: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, input=input_text, text=True, capture_output=True, check=False)


def _run_tty(command: list[str], *, cwd: Path, env: dict[str, str], input_text: str) -> tuple[int, str]:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, text=False)
    finally:
        os.close(slave)
    os.write(master, input_text.encode())
    output = bytearray()
    try:
        while process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output.extend(os.read(master, 4096))
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
        while True:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            try:
                output.extend(os.read(master, 4096))
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
    finally:
        os.close(master)
    return process.wait(), output.decode(errors="replace")


@pytest.fixture
def reset_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str], Path]:
    clone = tmp_path / "clone"
    completed = subprocess.run(["git", "clone", "--quiet", "--shared", str(REPOSITORY), str(clone)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    script = clone / "scripts" / "first-run-reset.sh"
    shutil.copy2(REPOSITORY / "scripts" / "first-run-reset.sh", script)
    script.chmod(0o755)
    shutil.copy2(REPOSITORY / "scripts" / "first_run_reset_helper.py", clone / "scripts" / "first_run_reset_helper.py")

    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name in ("systemctl", "loginctl"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    env = os.environ.copy()
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(xdg), "PATH": f"{fake_bin}:{env['PATH']}", "PYTHONDONTWRITEBYTECODE": "1"})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return clone, env, script


def _snapshot(paths: list[Path]) -> dict[Path, tuple[bytes | None, int]]:
    return {
        path: (None if path.is_dir() else path.read_bytes(), path.stat().st_mode)
        for path in paths
    }


def _instance_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def _render_unit(root: Path) -> str:
    return f"[Service]\nWorkingDirectory={root}\n"


def _owned_section(stdout: str) -> str:
    return stdout.split("対象:", 1)[1].split("触らない:", 1)[0]


def _excluded_section(stdout: str) -> str:
    return stdout.split("触らない:", 1)[1]


def _write_feature_config(root: Path, env: dict[str, str]) -> None:
    code = "from pathlib import Path; from agentic_fx.ops_setup.feature_config import unsafe_reason, write_inputs; root = Path(__import__('sys').argv[1]); assert unsafe_reason(root, 'web') is None, unsafe_reason(root, 'web'); write_inputs(root, 'web', {'port': 8788})"
    completed = subprocess.run([sys.executable, "-c", code, str(root)], env=env, cwd=REPOSITORY, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def _populate_owned_and_foreign(clone: Path, env: dict[str, str], tmp_path: Path) -> list[Path]:
    (clone / "config" / "settings.yaml").write_text("schema_version: 1\n")
    (clone / "config" / "settings.yaml.bak-old").write_text("old\n")
    (clone / "data" / "state").mkdir(parents=True)
    (clone / "data" / "ops").mkdir()
    (clone / "data" / "state" / "setup.json").write_text('{"linger": {"state": "owned_by_wizard"}}\n')
    (clone / "logs").mkdir()
    (clone / "logs" / "activity.log").write_text("log\n")
    (clone / "plugins").mkdir(exist_ok=True)
    (clone / "plugins" / "generated.py").write_text("generated\n")
    (clone / "policy" / "directives.md").write_text("- directive\n")
    (clone / ".env").write_text("SECRET=value\n")
    _write_feature_config(clone, env)

    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "afx.service").write_text(_render_unit(clone))
    foreign_root = tmp_path / "foreign-root"
    foreign_root.mkdir()
    foreign_unit = unit_dir / "afx.service.bak-foreign"
    foreign_unit.write_text(_render_unit(foreign_root))
    (unit_dir / "afx-ops@.service").write_text("template\n")
    dropin = unit_dir / "afx.service.d" / "afx-ops.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Unit]\n")

    own_key = Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone)
    own_key.mkdir(parents=True)
    (own_key / "operator.token").write_text("owned\n")
    foreign_key = own_key.parent / "different-instance"
    foreign_key.mkdir()
    foreign_key_file = foreign_key / "operator.token"
    foreign_key_file.write_text("foreign\n")

    foreign_feature = Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "foreign"
    (foreign_feature / "config").mkdir(parents=True)
    foreign_config = foreign_feature / "config" / "ops.toml"
    foreign_config.write_text(f'instance_root = "{foreign_root}"\n')
    foreign_secret = foreign_feature / "secret" / "token"
    foreign_secret.parent.mkdir()
    foreign_secret.write_text("foreign\n")
    return [foreign_unit, foreign_key, foreign_key_file, foreign_feature, foreign_config, foreign_secret]


def _git_baseline(root: Path) -> bytes:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--ignored=matching"],
        cwd=root, capture_output=True, check=False,
    )
    assert completed.returncode == 0
    return completed.stdout


def test_lists_every_owned_path_and_keeps_foreign_assets_unchanged(reset_environment, tmp_path):
    clone, env, script = reset_environment
    foreign_paths = _populate_owned_and_foreign(clone, env, tmp_path)
    before = _snapshot(foreign_paths)

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert "対象:" in result.stdout
    assert "触らない:" in result.stdout
    assert "paper 取引履歴、注文、approval、改善 report、RAG、plugins を永久削除します" in result.stdout
    for path in (
        clone / "config" / "settings.yaml",
        clone / "config" / "settings.yaml.bak-old",
        clone / "data",
        clone / "logs",
        clone / "plugins",
        clone / "policy" / "directives.md",
        Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "afx.service",
        Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "afx-ops@.service",
        Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "afx.service.d" / "afx-ops.conf",
        Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone),
        Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web",
    ):
        assert str(path) in result.stdout
    assert _snapshot(foreign_paths) == before


def test_confirmation_paths_and_dry_run_leave_everything_in_place(reset_environment, tmp_path):
    clone, env, script = reset_environment
    foreign_paths = _populate_owned_and_foreign(clone, env, tmp_path)
    tracked = [clone / "config" / "settings.yaml", clone / "data" / "state" / "setup.json", *foreign_paths]
    before = _snapshot(tracked)

    no_yes = _run([str(script), "--root", str(clone)], cwd=tmp_path, env=env)
    cancelled_rc, cancelled_output = _run_tty([str(script), "--root", str(clone)], cwd=tmp_path, env=env, input_text="not-reset\n")
    dry_run = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)
    included = _run([str(script), "--root", str(clone), "--dry-run", "--include-env", "--include-linger"], cwd=tmp_path, env=env)
    confirmed_rc, confirmed_output = _run_tty([str(script), "--root", str(clone)], cwd=tmp_path, env=env, input_text="reset\nstop\n")
    accepted = _run([str(script), "--root", str(clone), "--yes", "--stop-running"], cwd=tmp_path, env=env)

    assert no_yes.returncode == 2
    assert cancelled_rc == 0
    assert "削除は未実装です" not in cancelled_output
    assert confirmed_rc == 0
    assert "git status --porcelain --ignored:" in confirmed_output
    assert dry_run.returncode == 0
    assert included.returncode == 0
    assert str(clone / ".env") in included.stdout
    assert "linger" in included.stdout
    assert accepted.returncode == 0
    assert "git status --porcelain --ignored:" in accepted.stdout
    assert not (clone / "config" / "settings.yaml").exists()


def test_empty_root_and_external_symlink_are_safe(reset_environment, tmp_path):
    clone, env, script = reset_environment
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "config").mkdir()
    shutil.copy2(clone / "config" / "settings.yaml.example", empty / "config" / "settings.yaml.example")
    subprocess.run(["git", "init", "--quiet", str(empty)], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "settings.yaml"
    outside_file.write_text("do not follow\n")
    (empty / "config" / "settings.yaml").symlink_to(outside_file)

    result = _run([str(script), "--root", str(empty), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert "対象外 (symlink)" in result.stdout
    assert outside_file.read_text() == "do not follow\n"


def test_rejects_all_control_characters_in_root(reset_environment, tmp_path):
    clone, env, script = reset_environment
    unsafe = tmp_path / "unsafe\x1broot"
    subprocess.run(["git", "clone", "--quiet", str(clone), str(unsafe)], check=True)

    result = _run([str(script), "--root", str(unsafe), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 2
    assert "制御文字" in result.stderr


def test_matches_rendered_unit_paths_and_writer_owned_feature_with_special_root(reset_environment, tmp_path):
    clone, env, script = reset_environment
    special = tmp_path / 'root"with\\escapes'
    subprocess.run(["git", "clone", "--quiet", str(clone), str(special)], check=True)
    _write_feature_config(special, env)
    plain = tmp_path / "plainroot"
    subprocess.run(["git", "clone", "--quiet", str(clone), str(plain)], check=True)
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    from agentic_fx.setup.unit import render_unit
    rendered = render_unit(plain, plain / ".venv" / "bin" / "python", ["/usr/bin"], "C.UTF-8", False)
    (unit_dir / "afx.service").write_text(rendered.replace(f"WorkingDirectory={plain}", f'WorkingDirectory="{plain}/"'))
    (unit_dir / "afx.service.bak-owned").write_text(rendered.replace(f"WorkingDirectory={plain}", f"WorkingDirectory={plain}/"))

    unit_result = _run([str(script), "--root", str(plain), "--dry-run"], cwd=tmp_path, env=env)
    result = _run([str(script), "--root", str(special), "--dry-run"], cwd=tmp_path, env=env)

    assert unit_result.returncode == 0, unit_result.stderr
    assert str(unit_dir / "afx.service") in _owned_section(unit_result.stdout)
    assert str(unit_dir / "afx.service.bak-owned") in _owned_section(unit_result.stdout)
    assert unit_result.stdout.count("WorkingDirectory がこの root と一致する unit") == 2
    assert result.returncode == 0, result.stderr
    web = str(Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web")
    assert web in _owned_section(result.stdout)
    assert web not in _excluded_section(result.stdout)


def test_unknown_or_unreadable_feature_configs_are_excluded(reset_environment, tmp_path):
    clone, env, script = reset_environment
    ops = Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops"
    for name, contents in (("missing", "port = 8788\n"), ("multiple", f'instance_root = "{clone}"\ninstance_root = "{clone}"\n'), ("other-format", f"instance_root='{clone}'\n")):
        config = ops / name / "config"
        config.mkdir(parents=True)
        (config / "ops.toml").write_text(contents)
    unreadable = ops / "unreadable" / "config"
    unreadable.mkdir(parents=True)
    unreadable_config = unreadable / "ops.toml"
    unreadable_config.write_bytes(b"\xff")
    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert "所有不明または別 instance" in result.stdout
    assert "判定できない" in result.stdout


def test_uses_home_config_for_keys_and_normalizes_relative_and_symlink_roots(reset_environment, tmp_path):
    clone, env, script = reset_environment
    env.pop("XDG_CONFIG_HOME")
    home_key = Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone)
    home_key.mkdir(parents=True)
    link = tmp_path / "root-link"
    link.symlink_to(clone, target_is_directory=True)

    result = _run([str(script), "--root", "root-link", "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert f"root: {clone.resolve()}" in result.stdout
    assert str(home_key) in result.stdout

    env["XDG_CONFIG_HOME"] = "relative-config"
    unit = Path(env["HOME"]) / ".config" / "systemd" / "user" / "afx.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(_render_unit(clone))
    relative_xdg = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)
    assert relative_xdg.returncode == 0, relative_xdg.stderr
    assert str(unit) in relative_xdg.stdout


def test_symlinked_candidates_are_all_excluded_and_plan_preserves_git_baseline(reset_environment, tmp_path):
    clone, env, script = reset_environment
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("data", "logs", "plugins", ".env"):
        target = outside / name
        target.write_text(name)
        (clone / name).symlink_to(target)
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "afx.service"
    unit.parent.mkdir(parents=True)
    unit.symlink_to(outside / "unit")
    key = Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone)
    key.parent.mkdir(parents=True)
    key.symlink_to(outside / "key")
    feature = Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web"
    feature.parent.mkdir(parents=True)
    feature.symlink_to(outside / "feature")
    before = _git_baseline(clone)

    result = _run([str(script), "--root", str(clone), "--dry-run", "--include-env"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("対象外 (symlink") >= 7
    assert _git_baseline(clone) == before


def _bare_root(tmp_path: Path, clone: Path, name: str) -> Path:
    bare = tmp_path / name
    (bare / "config").mkdir(parents=True)
    shutil.copy2(clone / "config" / "settings.yaml.example", bare / "config" / "settings.yaml.example")
    subprocess.run(["git", "init", "--quiet", str(bare)], check=True)
    return bare


@pytest.mark.parametrize("name", ['quote"root', "back\\slash", 'both"and\\root'])
def test_feature_written_by_real_writer_is_owned_for_quote_and_backslash_roots(reset_environment, tmp_path, name):
    clone, env, script = reset_environment
    special = tmp_path / name
    subprocess.run(["git", "clone", "--quiet", str(clone), str(special)], check=True)
    _write_feature_config(special, env)

    result = _run([str(script), "--root", str(special), "--dry-run"], cwd=tmp_path, env=env)

    web = str(Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web")
    assert result.returncode == 0, result.stderr
    assert web in _owned_section(result.stdout)
    assert web not in _excluded_section(result.stdout)


def test_unit_working_directory_with_systemd_escapes_matches_root(reset_environment, tmp_path):
    clone, env, script = reset_environment
    spaced = tmp_path / "fx root"
    subprocess.run(["git", "clone", "--quiet", str(clone), str(spaced)], check=True)
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    escaped = str(spaced).replace(" ", "\\x20")
    (unit_dir / "afx.service").write_text(f"[Service]\nWorkingDirectory={escaped}\n")
    (unit_dir / "afx.service.bak-a").write_text(f"[Service]\nWorkingDirectory={str(spaced).replace(' ', chr(92) + ' ')}\n")
    (unit_dir / "afx.service.bak-b").write_text(f"[Service]\nWorkingDirectory={str(tmp_path)}/fx\\troot\n")

    result = _run([str(script), "--root", str(spaced), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert str(unit_dir / "afx.service") in _owned_section(result.stdout)
    assert str(unit_dir / "afx.service.bak-a") in _owned_section(result.stdout)
    assert str(unit_dir / "afx.service.bak-b") in _excluded_section(result.stdout)


def test_feature_config_dir_symlinked_outside_root_is_excluded(reset_environment, tmp_path):
    clone, env, script = reset_environment
    outside = tmp_path / "outside-config"
    outside.mkdir()
    (outside / "ops.toml").write_text(f'instance_root = "{clone.resolve()}"\n')
    feature = Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web"
    feature.mkdir(parents=True)
    (feature / "config").symlink_to(outside, target_is_directory=True)

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert str(feature) not in _owned_section(result.stdout)
    assert f"{feature} (対象外 (symlink-out-of-root))" in _excluded_section(result.stdout)


def test_root_with_nothing_owned_or_excluded_succeeds(reset_environment, tmp_path):
    clone, env, script = reset_environment
    bare = _bare_root(tmp_path, clone, "bare")

    result = _run([str(script), "--root", str(bare), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert _owned_section(result.stdout).strip() == "(なし)"
    assert _excluded_section(result.stdout).split("paper", 1)[0].strip() == "(なし)"


def test_empty_root_argument_is_rejected(reset_environment, tmp_path):
    clone, env, script = reset_environment

    result = _run([str(script), "--root", "", "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 2
    assert "空" in result.stderr
    assert result.stdout == ""


def test_unit_working_directory_literal_escape_text_is_not_owned(reset_environment, tmp_path):
    clone, env, script = reset_environment
    literal = tmp_path / "fx\\x2fowned"
    subprocess.run(["git", "clone", "--quiet", str(clone), str(literal)], check=True)
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "afx.service").write_text(f"[Service]\nWorkingDirectory={literal}\n")

    result = _run([str(script), "--root", str(literal), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert str(unit_dir / "afx.service") not in _owned_section(result.stdout)
    assert str(unit_dir / "afx.service") in _excluded_section(result.stdout)


def test_api_key_directory_symlinked_outside_is_excluded(reset_environment, tmp_path):
    clone, env, script = reset_environment
    outside = tmp_path / "outside-api"
    (outside / _instance_id(clone)).mkdir(parents=True)
    base = Path(env["HOME"]) / ".config" / "agentic-fx"
    base.mkdir(parents=True)
    (base / "api").symlink_to(outside, target_is_directory=True)

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    own = str(base / "api" / _instance_id(clone))
    assert result.returncode == 0, result.stderr
    assert own not in _owned_section(result.stdout)
    assert f"{own} (対象外 (symlink-out-of-root))" in _excluded_section(result.stdout)


def test_ops_base_directory_symlinked_outside_is_excluded(reset_environment, tmp_path):
    clone, env, script = reset_environment
    outside = tmp_path / "outside-agentic-fx"
    (outside / "ops" / "web" / "config").mkdir(parents=True)
    (outside / "ops" / "web" / "config" / "ops.toml").write_text(f'instance_root = "{clone.resolve()}"\n')
    xdg = Path(env["XDG_CONFIG_HOME"])
    xdg.mkdir(parents=True)
    (xdg / "agentic-fx").symlink_to(outside, target_is_directory=True)

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    web = str(xdg / "agentic-fx" / "ops" / "web")
    assert result.returncode == 0, result.stderr
    assert web not in _owned_section(result.stdout)
    assert f"{web} (対象外 (symlink-out-of-root))" in _excluded_section(result.stdout)


def test_names_containing_pipe_are_listed_intact(reset_environment, tmp_path):
    clone, env, script = reset_environment
    feature = Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "we|b"
    (feature / "config").mkdir(parents=True)
    (feature / "config" / "ops.toml").write_text('instance_root = "/elsewhere"\n')
    foreign_key = Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / "k|ey"
    foreign_key.mkdir(parents=True)

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert f"{feature} (所有不明または別 instance の資材を検出、触らない)" in _excluded_section(result.stdout)
    assert f"{foreign_key} (別 instance の鍵を検出、触らない)" in _excluded_section(result.stdout)


def test_usage_describes_running_instance_confirmation(reset_environment, tmp_path):
    clone, env, script = reset_environment

    usage = _run([str(script), "--help"], cwd=tmp_path, env=env)
    run = _run([str(script), "--root", str(clone), "--yes"], cwd=tmp_path, env=env)

    assert "--stop-running" in usage.stdout
    assert "git status --porcelain --ignored:" in run.stdout


@pytest.mark.parametrize(
    "line",
    ["WorkingDirectory=.", "WorkingDirectory=", "WorkingDirectory=~", "WorkingDirectory=-{root}", "WorkingDirectory=rel/dir"],
)
def test_unit_working_directory_not_absolute_is_not_owned(reset_environment, tmp_path, line):
    clone, env, script = reset_environment
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "afx.service").write_text(f"[Service]\n{line.format(root=clone)}\n")

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=clone, env=env)

    assert result.returncode == 0, result.stderr
    assert str(unit_dir / "afx.service") not in _owned_section(result.stdout)


def test_unit_with_root_directory_is_not_owned_but_plain_absolute_is(reset_environment, tmp_path):
    clone, env, script = reset_environment
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "afx.service").write_text(f"[Service]\nRootDirectory=/srv/foreign\nWorkingDirectory={clone}\n")
    (unit_dir / "afx.service.bak-a").write_text(f"[Service]\nWorkingDirectory={clone}/\n")

    result = _run([str(script), "--root", str(clone), "--dry-run"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    owned_paths = [line.strip().split(" (")[0] for line in _owned_section(result.stdout).splitlines()]
    assert str(unit_dir / "afx.service") not in owned_paths
    assert str(unit_dir / "afx.service.bak-a") in owned_paths


@pytest.mark.parametrize(
    "arguments",
    [["root", ""], ["contained", "", ""], ["contained", "/tmp", ""], ["unit-match", "", "/dev/null"], ["feature-match", "/tmp", ""], ["linger", ""]],
)
def test_helper_rejects_empty_arguments(arguments):
    helper = Path(__file__).resolve().parents[2] / "scripts" / "first_run_reset_helper.py"

    result = subprocess.run([sys.executable, "-I", str(helper), *arguments], capture_output=True, text=True)

    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1

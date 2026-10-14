from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "collect-first-run-diagnostics.sh"

SECRET = "SECRETVALUE-9f3a"
URL_HOST = "llama-private.example"

#: 診断が実行してよい外部コマンドの全体。これ以外 (claude / codex / uv など) は PATH に置かない。
TOOLS = ("bash", "sh", "env", "tar", "gzip", "sed", "awk", "grep", "cat", "ls", "find", "stat", "uname", "date",
         "mktemp", "timeout", "readlink", "head", "tail", "sort", "id", "dirname", "basename", "rm", "mkdir",
         "python3", "git", "tr", "wc", "cut", "tee", "true", "false", "sleep", "xargs", "uniq", "printf", "test")

FAKE_SYSTEMCTL = r"""#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
case "$*" in
  "--user cat afx.service")
    echo "# /home/test/.config/systemd/user/afx.service"
    echo "[Service]"
    echo "Type=exec"
    echo "Environment=API_TOKEN=SECRETVALUE-9f3a"
    echo "ExecStart=/srv/afx/.venv/bin/python main.py --daemon"
    ;;
  *"show afx.service"*"-p MainPID"*|*"show afx.service -p MainPID"*)
    echo "MainPID=$$"
    echo "Type=exec"
    echo "ControlGroup=/user.slice/user-1000.slice/user@1000.service/app.slice/afx.service"
    ;;
  *"InvocationID --value"*) echo "0123456789abcdef0123456789abcdef" ;;
  *"list-units"*)
    printf '%s\n' "● afx-ops@web.service loaded failed failed afx-ops web"
    printf '%s\n' "* afx-ops@bot.service loaded failed failed afx-ops bot"
    printf '%s\n' "  afx-ops@api.service loaded active running afx-ops api"
    ;;
  *"is-active"*) echo "active" ;;
  *"is-enabled"*) echo "enabled" ;;
  --version) echo "systemd 249 (249.11-0ubuntu3)" ;;
  *"show afx"*|*"show -p"*) echo "Wants=network-online.target"; echo "After=network-online.target"; echo "NRestarts=0" ;;
  *) echo "unhandled: $*" ;;
esac
"""

FAKE_JOURNALCTL = r"""#!/bin/sh
echo "journalctl $*" >> "$FAKE_LOG"
case "$*" in
  *"-o json"*)
    echo '{"MESSAGE":"improve isolation: enabled; backend=claude admitted=yes","_SYSTEMD_INVOCATION_ID":"0123456789abcdef0123456789abcdef"}'
    echo '{"MESSAGE":"fetched http://llama-private.example/v1 ok","USER_INVOCATION_ID":"fedcba9876543210fedcba9876543210"}'
    ;;
  *"--show-cursor"*) echo "-- cursor: s=abc;i=1;b=2;m=3;t=4;x=5" ;;
  *"-o cat"*) echo "improve isolation: enabled; backend=claude admitted=yes" ;;
  *) echo "Oct 14 12:00:00 host afx[1]: GET http://llama-private.example/v1 KEY=SECRETVALUE-9f3a"
     echo "Oct 14 12:00:01 host afx[1]: Authorization: Bearer BEARERVALUE-abc123"
     echo "Oct 14 12:00:02 host afx[1]: retry with Bearer ONLYBEARER-777 again"
     echo "Oct 14 12:00:03 host afx[1]: OPS_AUTH=OPSAUTHVALUE-xyz started"
     echo "Oct 14 12:00:04 host afx[1]: db_pass = 'PASSVALUE-q1' ok"
     ;;
esac
"""

FAKE_LOGINCTL = r"""#!/bin/sh
echo "loginctl $*" >> "$FAKE_LOG"
echo "Linger=yes"
"""


def _tool_farm(directory: Path, *, fakes: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for tool in TOOLS:
        found = shutil.which(tool)
        if found:
            (directory / tool).symlink_to(found)
    if fakes:
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("journalctl", FAKE_JOURNALCTL),
                           ("loginctl", FAKE_LOGINCTL)):
            path = directory / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
    return directory


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "clone"
    (root / "scripts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "settings.yaml.example").write_text("pairs: [USDJPY]\n", encoding="utf-8")
    shutil.copy2(SCRIPT, root / "scripts" / SCRIPT.name)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    home = tmp_path / "home"
    home.mkdir()
    return {"root": root, "home": home, "bin": _tool_farm(tmp_path / "bin", fakes=True),
            "log": tmp_path / "fake.log", "tmp": tmp_path}


def _environment(workspace: dict[str, Path], *, path: Path | None = None) -> dict[str, str]:
    scratch = workspace["tmp"] / "scratch"
    scratch.mkdir(exist_ok=True)
    return {"PATH": str(path or workspace["bin"]), "HOME": str(workspace["home"]),
            "XDG_CONFIG_HOME": str(workspace["home"] / ".config"), "TMPDIR": str(scratch),
            "FAKE_LOG": str(workspace["log"]), "USER": "tester", "LANG": "C.UTF-8"}


def _run(workspace: dict[str, Path], *arguments: str, environment: dict[str, str] | None = None,
         use_copy: bool = True) -> subprocess.CompletedProcess[str]:
    script = workspace["root"] / "scripts" / SCRIPT.name if use_copy else SCRIPT
    return subprocess.run(["bash", str(script), *arguments], capture_output=True, text=True, timeout=120,
                          env=environment or _environment(workspace), check=False, cwd=workspace["tmp"])


def _archive(workspace: dict[str, Path]) -> Path:
    found = sorted((workspace["root"] / "tmp" / "acceptance").glob("diag-*.tar.gz"))
    assert len(found) == 1, found
    return found[0]


def _contents(archive: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                data = tar.extractfile(member)
                assert data is not None
                result[member.name] = data.read().decode("utf-8", errors="replace")
    return result


def test_bash_syntax_is_valid() -> None:
    completed = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_script_is_executable_and_strict_about_unset_variables() -> None:
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -u" in text
    assert "agentic_fx" not in text.replace("agentic-fx", "")


def test_collects_one_tarball_with_a_file_per_observation(workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    contents = _contents(_archive(workspace))
    names = sorted(name.split("/", 1)[1] for name in contents)
    for expected in ("01-effective-unit.txt", "02-cgroup.txt", "03-linger.txt", "04-journal-contract.txt",
                     "05-exit-status.txt", "06-dependencies.txt", "07-ops-ready.txt", "08-ops-restart-link.txt",
                     "09-reset-state.txt", "10-environment.txt", "11-journal-tail.txt", "12-setup-record.txt",
                     "13-tools.txt"):
        assert expected in names, names
    assert len({name.split("/", 1)[0] for name in contents}) == 1
    unit = next(text for name, text in contents.items() if name.endswith("01-effective-unit.txt"))
    assert "Type=exec" in unit and "main.py --daemon" in unit
    contract = next(text for name, text in contents.items() if name.endswith("04-journal-contract.txt"))
    assert "_SYSTEMD_INVOCATION_ID" in contract and "USER_INVOCATION_ID" in contract
    assert "improve isolation" in contract


def test_prints_the_collection_list_before_collecting(workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0
    head = completed.stdout.split("diag-", 1)[0]
    assert "収集しない" in head
    for excluded in (".env", "config/settings.yaml", "secret"):
        assert excluded in head


def test_secret_files_and_urls_never_enter_the_archive(workspace: dict[str, Path]) -> None:
    root, home = workspace["root"], workspace["home"]
    (root / ".env").write_text(f"MT5_BRIDGE_API_KEY={SECRET}\n", encoding="utf-8")
    (root / "config" / "settings.yaml").write_text(
        f"llama_swap:\n  base_url: http://{URL_HOST}/v1\n", encoding="utf-8")
    (root / "data" / "state").mkdir(parents=True)
    (root / "data" / "state" / "setup.json").write_text(
        '{"price": {"result": "ok", "reason_code": "user_declined_probe", "at": "2026-10-14T00:00:00+00:00"},'
        f' "note": "http://{URL_HOST}/v1 {SECRET}", "unit": {{"state": "installed", "free": "{SECRET}"}}}}',
        encoding="utf-8")
    secret_dir = home / ".config" / "agentic-fx" / "ops" / "bot" / "secret"
    secret_dir.mkdir(parents=True)
    (secret_dir / "token").write_text(SECRET, encoding="utf-8")
    api_dir = home / ".config" / "agentic-fx" / "api" / "0123456789abcdef"
    api_dir.mkdir(parents=True)
    (api_dir / "operator.key").write_text(SECRET, encoding="utf-8")
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    contents = _contents(_archive(workspace))
    for name, text in contents.items():
        assert SECRET not in text, name
        assert URL_HOST not in text, name
        assert "http://" not in text, name
    assert not any(name.endswith((".env", "settings.yaml", "token", "operator.key")) for name in contents)
    record = next(text for name, text in contents.items() if name.endswith("12-setup-record.txt"))
    assert "user_declined_probe" in record and "installed" in record
    assert "price" in record and "result" in record
    assert "2026-10-14" not in record


def test_works_without_env_or_data_or_settings(workspace: dict[str, Path]) -> None:
    assert not (workspace["root"] / ".env").exists() and not (workspace["root"] / "data").exists()
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    record = next(text for name, text in _contents(_archive(workspace)).items()
                  if name.endswith("12-setup-record.txt"))
    assert "取得できず" in record


def test_missing_systemd_tools_are_recorded_and_the_run_still_succeeds(workspace: dict[str, Path]) -> None:
    bare = _tool_farm(workspace["tmp"] / "bare-bin", fakes=False)
    completed = _run(workspace, environment=_environment(workspace, path=bare))
    assert completed.returncode == 0, completed.stderr
    contents = _contents(_archive(workspace))
    for suffix in ("01-effective-unit.txt", "03-linger.txt", "04-journal-contract.txt", "11-journal-tail.txt"):
        text = next(text for name, text in contents.items() if name.endswith(suffix))
        assert "取得できず" in text, suffix


def test_failing_systemctl_and_journalctl_do_not_fail_the_run(workspace: dict[str, Path]) -> None:
    for name in ("systemctl", "journalctl", "loginctl"):
        path = workspace["bin"] / name
        path.write_text('#!/bin/sh\necho "Failed to connect to bus" >&2\nexit 1\n', encoding="utf-8")
        path.chmod(0o755)
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    unit = next(text for name, text in _contents(_archive(workspace)).items() if name.endswith("01-effective-unit.txt"))
    assert "取得できず" in unit


def test_root_defaults_to_the_script_location_and_out_overrides(workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    assert (workspace["root"] / "tmp" / "acceptance").is_dir()
    target = workspace["tmp"] / "elsewhere" / "diag.tar.gz"
    target.parent.mkdir()
    completed = _run(workspace, "--root", str(workspace["root"]), "--out", str(target))
    assert completed.returncode == 0, completed.stderr
    assert tarfile.is_tarfile(target)


def test_only_systemd_read_commands_are_issued(workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    issued = workspace["log"].read_text(encoding="utf-8").splitlines()
    assert issued
    forbidden = ("start", "stop", "restart", "enable", "disable", "daemon-reload", "reset-failed", "kill",
                 "enable-linger", "disable-linger")
    for line in issued:
        words = line.split()
        assert not any(word in forbidden for word in words), line


def test_unknown_argument_is_a_usage_error_without_collecting(workspace: dict[str, Path]) -> None:
    completed = _run(workspace, "--bogus")
    assert completed.returncode == 2
    assert not (workspace["root"] / "tmp").exists()


def test_does_not_leave_its_working_directory_behind(workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    assert list((workspace["tmp"] / "scratch").iterdir()) == []


def test_every_ops_feature_is_collected_even_when_the_listing_starts_with_a_state_mark(
        workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    contents = _contents(_archive(workspace))
    ready = next(text for name, text in contents.items() if name.endswith("07-ops-ready.txt"))
    for feature in ("web", "bot", "api"):
        assert f"is-active afx-ops@{feature}.service" in ready, feature
    restart = next(text for name, text in contents.items() if name.endswith("08-ops-restart-link.txt"))
    for feature in ("web", "bot", "api"):
        assert f"afx-ops@{feature}.service" in restart, feature
    issued = workspace["log"].read_text(encoding="utf-8")
    assert "list-units" in issued and "--plain" in issued


def test_authorization_bearer_and_auth_style_values_never_enter_the_archive(workspace: dict[str, Path]) -> None:
    completed = _run(workspace)
    assert completed.returncode == 0, completed.stderr
    contents = _contents(_archive(workspace))
    tail = next(text for name, text in contents.items() if name.endswith("11-journal-tail.txt"))
    assert "Authorization" in tail and "OPS_AUTH" in tail
    for name, text in contents.items():
        for leaked in ("BEARERVALUE-abc123", "ONLYBEARER-777", "OPSAUTHVALUE-xyz", "PASSVALUE-q1"):
            assert leaked not in text, (name, leaked)

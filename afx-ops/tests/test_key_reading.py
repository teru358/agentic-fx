"""鍵の読込み: 自分の principal の鍵だけを、manifest が許す場合だけ読む。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _keydir import build_key_dir, digest, token_for, write_ready
from _oracle_harness import no_core

from afx_ops import keys
from afx_ops.keys import KeyReadError, read_token

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _read(directory: Path, principal: str) -> str:
    return read_token(principal, key_file=directory / f"{principal}.token",
                      ready_file=directory / ".ready")


def _fails_with(directory: Path, principal: str) -> KeyReadError:
    with pytest.raises(KeyReadError) as info:
        _read(directory, principal)
    # 例外文に鍵の平文が無いこと (assert 失敗文にも出さないよう真偽だけ比べる)。
    leaked = any(token_for(p) in str(info.value) or token_for(p) in repr(info.value)
                 for p in ("ops-bot", "ops-web", "ops-notify"))
    assert leaked is False
    return info.value


def test_reads_own_principal_key_when_manifest_and_digest_agree(tmp_path):
    directory = build_key_dir(tmp_path / "keys")
    for principal in ("ops-bot", "ops-web", "ops-notify"):
        assert digest(_read(directory, principal)) == digest(token_for(principal))


def test_key_file_of_another_principal_is_refused(tmp_path):
    directory = build_key_dir(tmp_path / "keys")
    with pytest.raises(KeyReadError) as info:
        read_token("ops-web", key_file=directory / "ops-bot.token",
                   ready_file=directory / ".ready")
    assert info.value.code == "key_path_mismatch"
    for principal in ("operator", "approver"):
        assert _fails_with(directory, principal).code == "key_path_mismatch"


def test_manifest_outside_the_key_dir_is_refused(tmp_path):
    directory = build_key_dir(tmp_path / "keys")
    other = build_key_dir(tmp_path / "other")
    with pytest.raises(KeyReadError) as info:
        read_token("ops-web", key_file=directory / "ops-web.token",
                   ready_file=other / ".ready")
    assert info.value.code == "key_path_mismatch"


@pytest.mark.skipif(shutil.which("strace") is None, reason="strace が無い")
def test_reads_only_own_principal_key(tmp_path):
    directory = build_key_dir(tmp_path / "keys")
    trace = tmp_path / "trace"
    code = ("import sys; from pathlib import Path; from afx_ops.keys import read_token; "
            "d = Path(sys.argv[1]); "
            "t = read_token('ops-web', key_file=d / 'ops-web.token', ready_file=d / '.ready'); "
            "print(len(t))")
    done = subprocess.run(["strace", "-f", "-e", "trace=open,openat,openat2", "-o", str(trace),
                           sys.executable, "-I", "-c", code, str(directory)],
                          capture_output=True, text=True, timeout=60, preexec_fn=no_core)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "64"
    opened = set()
    for line in trace.read_text().splitlines():
        match = re.search(r'open(?:at2?)?\((?:\d+|AT_FDCWD), "([^"]*)"', line)
        if match:
            name = match.group(1)
            if name.startswith(str(directory)) or "/" not in name:
                opened.add(name)
    # dir 自身と、dir fd からの相対名 (.ready と自分の鍵) だけ。
    assert opened == {str(directory), ".ready", "ops-web.token"}


def _set_entry(directory: Path, principal: str, entry: dict | None) -> None:
    ready = json.loads((directory / ".ready").read_text())
    if entry is None:
        del ready["principals"][principal]
    else:
        ready["principals"][principal] = entry
    write_ready(directory, ready["principals"], version=ready["version"])


def _v1(directory: Path) -> None:
    (directory / ".ready").write_bytes((FIXTURES / "ready_v1.json").read_bytes())
    os.chmod(directory / ".ready", 0o600)


def _unregistered(directory: Path) -> None:
    _set_entry(directory, "ops-web", None)


def _preparing(directory: Path) -> None:
    _set_entry(directory, "ops-web", {"generation": 2, "state": "preparing", "op": "rotate"})


def _revoked(directory: Path) -> None:
    _set_entry(directory, "ops-web", {"generation": 2, "state": "revoked", "sha256": None})


def _digest_mismatch(directory: Path) -> None:
    _set_entry(directory, "ops-web", {"generation": 1, "state": "active",
                                      "sha256": digest(token_for("ops-bot"))})


def _key_mode_0644(directory: Path) -> None:
    os.chmod(directory / "ops-web.token", 0o644)


def _key_symlink(directory: Path) -> None:
    target = directory.parent / "elsewhere.token"
    os.replace(directory / "ops-web.token", target)
    (directory / "ops-web.token").symlink_to(target)


def _key_missing(directory: Path) -> None:
    os.unlink(directory / "ops-web.token")


def _dir_mode_0755(directory: Path) -> None:
    os.chmod(directory, 0o755)


def _dir_symlink(directory: Path) -> None:
    real = directory.parent / "real-keys"
    os.replace(directory, real)
    directory.symlink_to(real)


def _ready_mode_0644(directory: Path) -> None:
    os.chmod(directory / ".ready", 0o644)


def _unknown_principal(directory: Path) -> None:
    _set_entry(directory, "ops-mallory", {"generation": 1, "state": "active",
                                          "sha256": digest(token_for("x"))})


def _v1_with_ops_entry(directory: Path) -> None:
    ready = json.loads((FIXTURES / "ready_v1.json").read_text())
    ready["principals"]["ops-web"] = {"generation": 1, "state": "active",
                                      "sha256": digest(token_for("ops-web"))}
    write_ready(directory, ready["principals"], version=1)


def _not_initialized(directory: Path) -> None:
    os.unlink(directory / ".ready")


# (変更, 期待 code, manifest の段で止まるべきか)。manifest の段で止まるべきものは
# 鍵 file を開けない mode にしておき、開いていれば別の code になることで「読まない」を観測する。
STATES = {
    "v1_manifest_has_no_ops_principal": (_v1, "not_enrolled", True),
    "v1_manifest_with_ops_entry": (_v1_with_ops_entry, "ready_invalid", True),
    "v2_unregistered": (_unregistered, "not_enrolled", True),
    "v2_preparing": (_preparing, "ready_not_committed", True),
    "v2_revoked": (_revoked, "revoked", True),
    "v2_unknown_principal": (_unknown_principal, "ready_invalid", True),
    "ready_mode_0644": (_ready_mode_0644, "ready_invalid", True),
    "ready_missing": (_not_initialized, "not_initialized", True),
    "dir_mode_0755": (_dir_mode_0755, "key_dir_invalid", True),
    "dir_is_symlink": (_dir_symlink, "key_dir_invalid", True),
    "digest_mismatch": (_digest_mismatch, "digest_mismatch", False),
    "key_mode_0644": (_key_mode_0644, "key_file_invalid", False),
    "key_is_symlink": (_key_symlink, "key_file_invalid", False),
    "key_missing": (_key_missing, "key_missing", False),
}


@pytest.mark.parametrize("state", list(STATES))
def test_manifest_states_fail_closed(tmp_path, state):
    mutate, expected, before_key = STATES[state]
    directory = build_key_dir(tmp_path / "keys")
    if before_key and state != "dir_is_symlink":
        os.chmod(directory / "ops-web.token", 0o000)
    mutate(directory)
    assert _fails_with(directory, "ops-web").code == expected
    assert expected in keys.CODES


def test_manifest_states_cover_the_brief(tmp_path):
    assert {"v1_manifest_has_no_ops_principal", "v2_unregistered", "v2_preparing",
            "v2_revoked", "digest_mismatch", "key_mode_0644", "key_is_symlink"} <= set(STATES)

#!/usr/bin/env python3
"""first-run-reset.sh から呼ばれる判定 helper (標準ライブラリのみ)。

unit の WorkingDirectory は systemd の escape を解いた値だけで root と照合する
(unit renderer は escape が要る文字を含む root を置かないので、置ける root では
解いた値と書かれた値は一致する)。

各 subcommand は結果を 1 語で stdout に出す。引数不正や解決不能は rc 2。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import errno
import fcntl
from pathlib import Path


def _fail(message: str) -> "None":
    print(message, file=sys.stderr)
    raise SystemExit(2)


def cmd_root(argv: list[str]) -> None:
    try:
        root = Path(argv[0]).resolve()
    except (OSError, RuntimeError):
        _fail("root を実パスに解決できません")
    text = str(root)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        _fail("root に安全に扱えない制御文字が含まれます")
    print(text)
    print(hashlib.sha256(text.encode()).hexdigest()[:16])


_SIMPLE = {"\\": "\\", '"': '"', "'": "'", " ": " ", "t": "\t", "n": "\n", "r": "\r"}


def _unescape_systemd(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\" or index + 1 == len(value):
            result.append(character)
            index += 1
            continue
        following = value[index + 1]
        if following == "x" and index + 4 <= len(value):
            digits = value[index + 2:index + 4]
            try:
                result.append(chr(int(digits, 16)))
                index += 4
                continue
            except ValueError:
                pass
        if following in _SIMPLE:
            result.append(_SIMPLE[following])
        else:
            result.append(following)
        index += 2
    return "".join(result)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def cmd_unit_match(argv: list[str]) -> None:
    root, unit = argv
    try:
        value = None
        confined = False
        for line in Path(unit).read_text(encoding="utf-8").splitlines():
            if line.startswith("WorkingDirectory="):
                value = line.split("=", 1)[1]
            elif line.startswith(("RootDirectory=", "RootImage=")):
                if _unquote(line.split("=", 1)[1].strip()):
                    confined = True
        if value is None or confined:
            print("foreign")
            return
        working = _unescape_systemd(_unquote(value.strip()))
        if not working.startswith("/"):
            print("foreign")
            return
        if len(working) > 1:
            working = working.rstrip("/") or "/"
        owned = os.path.realpath(working) == os.path.realpath(root)
        print("owned" if owned else "foreign")
    except (OSError, UnicodeError, ValueError, RuntimeError):
        print("foreign")


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cmd_feature_match(argv: list[str]) -> None:
    root, config = argv
    canonical = "instance_root = " + _toml_string(str(Path(root).resolve()))
    try:
        lines = Path(config).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        print("unreadable")
        return
    print("owned" if sum(line == canonical for line in lines) == 1 else "foreign")


def cmd_linger(argv: list[str]) -> None:
    try:
        with open(argv[0], encoding="utf-8") as handle:
            value = json.load(handle)
        print("owned" if value.get("linger", {}).get("state") == "owned_by_wizard" else "no")
    except (OSError, ValueError, AttributeError):
        print("no")


def cmd_contained(argv: list[str]) -> None:
    base_argument, path_argument = argv
    base = Path(base_argument)
    path = Path(path_argument)
    try:
        relative = path.relative_to(base)
        expected = Path(os.path.realpath(base.parent)) / base.name
    except (OSError, RuntimeError, ValueError):
        print("symlink-out-of-root")
        return
    current = base
    for part in ("",) + relative.parts:
        if part:
            current = current / part
        if current.is_symlink():
            try:
                real = Path(os.path.realpath(current))
            except OSError:
                print("symlink-out-of-root")
                return
            if real != expected and expected not in real.parents:
                print("symlink-out-of-root")
                return
    print("ok")


def _try_lock(directory: int, name: str) -> tuple[str, int | None]:
    """Take an exclusive non-blocking flock on a regular lock file.

    A missing file is created, so that a process starting meanwhile meets the
    held lock instead of racing the deletion.  Returns ("ok", fd), ("busy",
    None), ("unsafe", None), or ("error", None)."""
    try:
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    except OSError as error:
        if error.errno == getattr(os, "ELOOP", 40):
            return "unsafe", None
        return "error", None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return "busy", None
    except OSError:
        os.close(descriptor)
        return "error", None
    return "ok", descriptor


def _directory_failure(error: OSError) -> str:
    """Verdict for a directory on the lock path that could not be opened without following links."""
    if error.errno in (getattr(os, "ELOOP", 40), errno.ENOTDIR):
        return "lock_path_unsafe"
    return "error"


def cmd_hold_locks(argv: list[str]) -> None:
    """Hold the setup lock and the instance lock until stdin is closed.

    The first line on stdout is the verdict: ok, setup-busy, instance-busy or
    error.  While the instance lock is busy the setup lock stays held; a
    "retry" line on stdin tries the instance lock again and prints a new
    verdict.  End of input releases everything and ends the process."""
    root, = argv
    descriptors: list[int] = []
    directory_flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        root_descriptor = os.open(root, directory_flags)
        descriptors.append(root_descriptor)
        data_descriptor = os.open("data", directory_flags, dir_fd=root_descriptor)
        descriptors.append(data_descriptor)
    except OSError as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        print(_directory_failure(error), flush=True)
        return
    ops_descriptor: int | None
    try:
        ops_descriptor = os.open("ops", directory_flags, dir_fd=data_descriptor)
    except FileNotFoundError:
        ops_descriptor = None  # no ops/ directory: nothing can hold setup.lock
    except OSError as error:
        print(_directory_failure(error), flush=True)
        return
    if ops_descriptor is not None:
        verdict, setup_descriptor = _try_lock(ops_descriptor, "setup.lock")
        if verdict != "ok":
            print("setup-busy" if verdict == "busy" else "lock_path_unsafe" if verdict == "unsafe" else "error", flush=True)
            return
    instance_descriptor: int | None = None
    held = False
    while True:
        if not held:
            verdict, instance_descriptor = _try_lock(data_descriptor, "instance.lock")
            if verdict != "ok" and verdict != "busy":
                print("lock_path_unsafe" if verdict == "unsafe" else "error", flush=True)
                return
            held = verdict == "ok"
        print("ok" if held else "instance-busy", flush=True)
        while True:
            line = sys.stdin.readline()
            if line == "":
                return
            if line.strip() == "retry":
                break
    # descriptors are released by process exit


_COMMANDS = {
    "root": (cmd_root, 1),
    "unit-match": (cmd_unit_match, 2),
    "feature-match": (cmd_feature_match, 2),
    "linger": (cmd_linger, 1),
    "contained": (cmd_contained, 2),
    "hold-locks": (cmd_hold_locks, 1),
}


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in _COMMANDS:
        _fail("usage: first_run_reset_helper.py {root|unit-match|feature-match|linger|contained|hold-locks} ARGS")
    function, count = _COMMANDS[argv[0]]
    if len(argv) - 1 != count:
        _fail("引数の数が違います")
    if any(not argument for argument in argv[1:]):
        _fail("空の引数は受け付けません")
    function(argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])

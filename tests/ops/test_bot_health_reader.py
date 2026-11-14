"""bot の health snapshot の reader の契約 (agentic_fx.ops.bot_health)。

両 package が共有する golden JSON (tests/fixtures/bot_health_contract.json) と hostile な file で
検査する。daemon 側 (agentic_fx.ops.bot_health) の test も同じ golden を読み、同じ正規化結果を返す。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.ops.bot_health import (MAX_BYTES, PERMANENT_CODES, SNAPSHOT_NAME, STATES, TRANSIENT_CODES,
                            evaluate, parse, read_health, read_raw, resolve_state_dir)

GOLDEN = json.loads(Path(__file__).resolve().parents[2].joinpath(
    "tests", "fixtures", "bot_health_contract.json").read_text(encoding="utf-8"))
NOW = datetime.strptime(GOLDEN["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
CASES = GOLDEN["cases"]
VALID = next(case for case in CASES if case["name"] == "healthy")["record"]


def _raw(case) -> bytes:
    return case["raw"].encode() if "raw" in case else json.dumps(case["record"]).encode()


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_golden_cases_normalize_identically(case):
    assert evaluate(parse(_raw(case)), NOW) == case["expect"]


def test_golden_constants_match_the_reader():
    assert sorted(PERMANENT_CODES) == GOLDEN["terminal_codes"]["permanent"]
    assert sorted(TRANSIENT_CODES) == GOLDEN["terminal_codes"]["transient"]
    assert list(STATES) == GOLDEN["states"]
    assert SNAPSHOT_NAME == GOLDEN["snapshot_name"] and MAX_BYTES == GOLDEN["max_bytes"]


@pytest.mark.parametrize("case", GOLDEN["xdg"], ids=[case["name"] for case in GOLDEN["xdg"]])
def test_state_dir_resolution_follows_the_golden(case):
    assert resolve_state_dir(case["env"], case["home"]) == Path(case["expect"])


def test_state_dir_resolution_defaults_to_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert resolve_state_dir() == tmp_path / "agentic-fx" / "ops" / "bot"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/dir")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_state_dir() == tmp_path / "home" / ".local" / "state" / "agentic-fx" / "ops" / "bot"


# ---- file の読み方 (hostile)

def _put(directory: Path, data: bytes) -> None:
    (directory / SNAPSHOT_NAME).write_bytes(data)


def _good() -> bytes:
    return json.dumps(VALID).encode()


def _padded(size: int) -> bytes:
    base = _good()
    assert size >= len(base)
    # 末尾の空白は JSON の外側 (内容は有効のまま大きさだけ変える)
    return base + b" " * (size - len(base))


def test_a_valid_file_is_read(tmp_path):
    _put(tmp_path, _good())
    assert read_health(tmp_path, NOW)["state"] == "healthy"


def test_a_missing_file_and_a_missing_directory_are_unknown(tmp_path):
    assert read_health(tmp_path, NOW)["state"] == "unknown"
    assert read_health(tmp_path / "nope", NOW)["state"] == "unknown"


def test_a_symlink_is_unknown_even_when_it_points_at_a_valid_file(tmp_path):
    (tmp_path / "real").write_bytes(_good())
    os.symlink("real", tmp_path / SNAPSHOT_NAME)
    assert read_raw(tmp_path) is None
    assert read_health(tmp_path, NOW)["state"] == "unknown"


def test_a_fifo_is_unknown_and_does_not_block(tmp_path):
    os.mkfifo(tmp_path / SNAPSHOT_NAME)
    assert read_health(tmp_path, NOW)["state"] == "unknown"


def test_a_directory_is_unknown(tmp_path):
    os.mkdir(tmp_path / SNAPSHOT_NAME)
    assert read_health(tmp_path, NOW)["state"] == "unknown"


def test_exactly_4096_bytes_is_accepted_and_4097_is_unknown(tmp_path):
    _put(tmp_path, _padded(4096))
    assert read_health(tmp_path, NOW)["state"] == "healthy"
    _put(tmp_path, _padded(4097))
    assert read_health(tmp_path, NOW)["state"] == "unknown"


def test_a_file_that_grows_after_the_size_check_is_unknown(tmp_path, monkeypatch):
    _put(tmp_path, _padded(5000))
    real = os.fstat

    def small(fd):
        info = real(fd)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid,
                               info.st_gid, 100, int(info.st_atime), int(info.st_mtime),
                               int(info.st_ctime)))
    monkeypatch.setattr(os, "fstat", small)
    assert read_health(tmp_path, NOW)["state"] == "unknown"


def test_a_file_is_read_with_exactly_one_read_of_4097_bytes(tmp_path, monkeypatch):
    _put(tmp_path, _good())
    calls = []
    real = os.read

    def spy(fd, count):
        calls.append(count)
        return real(fd, count)
    monkeypatch.setattr(os, "read", spy)
    read_health(tmp_path, NOW)
    assert calls == [4097]


def test_the_file_is_opened_without_following_symlinks_or_blocking(tmp_path, monkeypatch):
    _put(tmp_path, _good())
    seen = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        seen.append(flags)
        return real(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", spy)
    read_health(tmp_path, NOW)
    want = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    assert seen and all(flags & want == want for flags in seen)


@pytest.mark.parametrize("data", [b"{", b"[]", b"null", b"\xff\xfe", b"",
                                  json.dumps({**VALID, "x": 1}).encode()])
def test_broken_or_unexpected_content_is_unknown(tmp_path, data):
    _put(tmp_path, data)
    assert read_health(tmp_path, NOW)["state"] == "unknown"


@pytest.mark.skipif(os.geteuid() == 0, reason="root は読める")
def test_an_unreadable_file_is_unknown(tmp_path):
    _put(tmp_path, _good())
    (tmp_path / SNAPSHOT_NAME).chmod(0)
    assert read_health(tmp_path, NOW)["state"] == "unknown"


def test_the_result_has_only_the_fixed_fields(tmp_path):
    _put(tmp_path, _good())
    assert set(read_health(tmp_path, NOW)) == {"state", "observed_at", "gateway_state",
                                                "delivery_state", "terminal_code"}

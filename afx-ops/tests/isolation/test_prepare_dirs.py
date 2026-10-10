"""隔離前の領域の用意: 既存の config / state を所有者・mode・種別で確かめ、symlink を辿らない。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from _test_plans import test_plan
from afx_ops.isolation import startup
from afx_ops.isolation.startup import StartupError


def _plan(config: Path, state: Path) -> startup.StartupPlan:
    return test_plan("bot", config_dir=config, state_dir=state)


@pytest.fixture
def area(tmp_path):
    config = tmp_path / "config"
    config.mkdir(mode=0o755)
    config.chmod(0o755)
    secret = tmp_path / "secret"
    secret.mkdir(mode=0o700)
    (secret / "token").write_text("x")
    return tmp_path, config, tmp_path / "state", secret


def _reason(plan) -> str:
    with pytest.raises(StartupError) as info:
        startup.prepare_dirs(plan)
    return info.value.reason


def test_a_fresh_state_dir_is_created_0700_with_its_parents(area):
    base, config, _state, _secret = area
    state = base / "xs" / "agentic-fx" / "ops" / "bot"
    dirs = startup.prepare_dirs(_plan(config, state))
    try:
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert os.fstat(dirs.state_fd).st_ino == state.stat().st_ino
    finally:
        dirs.close()


def test_a_fresh_state_dir_is_0700_even_when_the_umask_drops_owner_bits(area):
    base, config, _state, _secret = area
    state = base / "xs" / "bot"
    state.parent.mkdir()
    old = os.umask(0o277)
    try:
        dirs = startup.prepare_dirs(_plan(config, state))
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        # 返る fd は検査した dir そのもの
        assert os.fstat(dirs.state_fd).st_ino == state.stat().st_ino
        assert os.fstat(dirs.config_fd).st_ino == config.stat().st_ino
    finally:
        dirs.close()


def test_an_existing_0700_state_dir_is_accepted(area):
    _base, config, state, _secret = area
    state.mkdir(mode=0o700)
    startup.prepare_dirs(_plan(config, state)).close()


def test_close_releases_both_fds(area):
    _base, config, state, _secret = area
    dirs = startup.prepare_dirs(_plan(config, state))
    fds = (dirs.config_fd, dirs.state_fd)
    dirs.close()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_a_state_symlink_to_the_secret_dir_stops_before_isolation(area):
    _base, config, state, secret = area
    state.symlink_to(secret)
    assert _reason(_plan(config, state)) == "state_dir_invalid"
    assert sorted(p.name for p in secret.iterdir()) == ["token"]
    assert stat.S_IMODE(secret.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777, 0o500])
def test_a_state_dir_with_a_mode_other_than_0700_stops(area, mode):
    _base, config, state, _secret = area
    state.mkdir()
    state.chmod(mode)
    try:
        assert _reason(_plan(config, state)) == "state_dir_invalid"
        # 既存の mode を直さない (直すと他者が開いたままの fd が残り得る)
        assert stat.S_IMODE(state.stat().st_mode) == mode
    finally:
        state.chmod(0o700)


def test_a_state_path_that_is_a_file_stops(area):
    _base, config, state, _secret = area
    state.write_text("")
    assert _reason(_plan(config, state)) == "state_dir_invalid"


def test_a_config_symlink_stops(area):
    base, config, state, secret = area
    link = base / "config-link"
    link.symlink_to(secret)
    assert _reason(_plan(link, state)) == "config_dir_invalid"
    assert not state.exists()


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
def test_a_config_dir_writable_by_others_stops(area, mode):
    _base, config, state, _secret = area
    config.chmod(mode)
    assert _reason(_plan(config, state)) == "config_dir_invalid"


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755, 0o500])
def test_a_config_dir_not_writable_by_others_is_accepted(area, mode):
    _base, config, state, _secret = area
    config.chmod(mode)
    try:
        startup.prepare_dirs(_plan(config, state)).close()
    finally:
        config.chmod(0o755)


def test_a_missing_config_dir_keeps_its_own_reason(area):
    base, _config, state, _secret = area
    assert _reason(_plan(base / "absent", state)) == "config_dir_missing"
    assert _reason(_plan(base / "absent" / "deeper", state)) == "config_dir_missing"


def test_a_config_path_that_is_a_file_stops(area):
    base, _config, state, _secret = area
    (base / "file").write_text("")
    assert _reason(_plan(base / "file", state)) == "config_dir_invalid"


@pytest.mark.skipif(os.getuid() == 0, reason="root では他 uid の dir を用意できない")
def test_dirs_owned_by_another_uid_stop(area):
    _base, config, state, _secret = area
    # /usr は root 所有 (この試験 process とは別 uid) の既存 dir
    other = Path("/usr")
    assert other.stat().st_uid != os.getuid()
    assert _reason(_plan(other, state)) == "config_dir_invalid"
    assert _reason(_plan(config, other)) == "state_dir_invalid"

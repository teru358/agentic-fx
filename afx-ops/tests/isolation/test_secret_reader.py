"""機能の secret は plan の secret dir から、plan が固定した名前の file だけを読む。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from _test_plans import test_plan
from afx_ops.isolation import startup
from afx_ops.isolation.startup import PLANS, StartupError


def _secret_dir(base: Path, files: dict[str, str]) -> Path:
    d = base / "secret"
    d.mkdir(mode=0o700)
    d.chmod(0o700)
    for name, text in files.items():
        (d / name).write_text(text)
        (d / name).chmod(0o600)
    return d


def _reason(plan) -> str:
    with pytest.raises(StartupError) as info:
        startup.read_secrets(plan)
    return info.value.reason


def test_every_feature_has_a_fixed_set_of_plain_secret_names():
    for feature, fixed in PLANS.items():
        assert isinstance(fixed.secret_files, tuple)
        for name in fixed.secret_files:
            assert name and "/" not in name and name not in (".", "..")
    assert PLANS["bot"].secret_files and PLANS["notify"].secret_files


def test_the_listed_files_are_read_and_nothing_else(tmp_path):
    d = _secret_dir(tmp_path, {"a.token": "A", "b.token": "B", "other.token": "X"})
    plan = test_plan("bot", secret_dir=d, secret_files=("a.token", "b.token"))
    assert dict(startup.read_secrets(plan)) == {"a.token": b"A", "b.token": b"B"}


def test_a_feature_without_secret_files_does_not_touch_the_secret_dir(tmp_path):
    plan = test_plan("web", secret_dir=tmp_path / "absent", secret_files=())
    assert dict(startup.read_secrets(plan)) == {}
    assert not (tmp_path / "absent").exists()


def test_a_symlinked_secret_dir_is_refused(tmp_path):
    d = _secret_dir(tmp_path, {"a.token": "A"})
    link = tmp_path / "link"
    link.symlink_to(d)
    assert _reason(test_plan("bot", secret_dir=link, secret_files=("a.token",))) == \
        "secrets_failed"


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o711])
def test_a_secret_dir_other_than_0700_is_refused(tmp_path, mode):
    d = _secret_dir(tmp_path, {"a.token": "A"})
    d.chmod(mode)
    assert _reason(test_plan("bot", secret_dir=d, secret_files=("a.token",))) == \
        "secrets_failed"


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o400, 0o700])
def test_a_secret_file_other_than_0600_is_refused(tmp_path, mode):
    d = _secret_dir(tmp_path, {"a.token": "A"})
    (d / "a.token").chmod(mode)
    assert _reason(test_plan("bot", secret_dir=d, secret_files=("a.token",))) == \
        "secrets_failed"


def test_a_symlinked_secret_file_is_refused(tmp_path):
    d = _secret_dir(tmp_path, {"real": "A"})
    (d / "a.token").symlink_to(d / "real")
    assert _reason(test_plan("bot", secret_dir=d, secret_files=("a.token",))) == \
        "secrets_failed"


def test_a_missing_secret_file_or_dir_is_refused(tmp_path):
    d = _secret_dir(tmp_path, {})
    assert _reason(test_plan("bot", secret_dir=d, secret_files=("a.token",))) == \
        "secrets_failed"
    assert _reason(test_plan("bot", secret_dir=tmp_path / "absent",
                             secret_files=("a.token",))) == "secrets_failed"


def test_a_secret_name_with_a_path_part_is_refused(tmp_path):
    d = _secret_dir(tmp_path, {"a.token": "A"})
    for name in ("../secret/a.token", "sub/a.token", "..", ""):
        assert _reason(test_plan("bot", secret_dir=d, secret_files=(name,))) == \
            "secrets_failed"


def test_the_failure_message_carries_neither_path_nor_content(tmp_path):
    d = _secret_dir(tmp_path, {"a.token": "very-secret"})
    (d / "a.token").chmod(0o644)
    with pytest.raises(StartupError) as info:
        startup.read_secrets(test_plan("bot", secret_dir=d, secret_files=("a.token",)))
    assert str(tmp_path) not in str(info.value) and "very-secret" not in str(info.value)


@pytest.mark.skipif(os.getuid() == 0, reason="root では他 uid の dir を用意できない")
def test_a_secret_dir_owned_by_another_uid_is_refused():
    assert _reason(test_plan("bot", secret_dir=Path("/usr"), secret_files=("x",))) == \
        "secrets_failed"

"""隔離後の tz: system の zoneinfo を探さず、pip の tzdata だけから換算できる。"""
from __future__ import annotations

import os
import socket
import sysconfig
import zoneinfo
from pathlib import Path

import pytest

from _afx_isolation_support import FEATURES, make_root, needs_landlock, needs_x86_64, run_canary
from afx_ops import cli
from afx_ops.config import OpsConfig
from afx_ops.isolation import startup

EXPECTED = {
    "tzpath": [],
    "new_york_summer": "2026-06-30 20:00:00 -04:00",
    "new_york_winter": "2026-11-30 19:00:00 -05:00",
    "japan": "2026-07-01 09:00:00 +09:00",
    "tokyo": "2026-07-01 09:00:00 +09:00",
}


@pytest.fixture
def root(tmp_path) -> Path:
    return make_root(tmp_path)


def _spec(root, feature):
    return {"feature": feature, "root": str(root), "op": "zone", "params": {}}


def _skip_without_loopback(feature):
    if feature != "web":
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("この sandbox は loopback listener の作成・bind を許可しない")


def _wrong_zone_dir(tmp_path: Path) -> Path:
    """system 側にあるはずの zone を、わざと別の規則 (Tokyo) にした探索先。"""
    system = next(Path(p) for p in ("/usr/share/zoneinfo", "/usr/lib/zoneinfo")
                  if (Path(p) / "Asia" / "Tokyo").exists())
    directory = tmp_path / "wrong-tz"
    (directory / "America").mkdir(parents=True)
    data = (system / "Asia" / "Tokyo").read_bytes()
    (directory / "America" / "New_York").write_bytes(data)
    (directory / "Japan").write_bytes(data)
    return directory


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_zones_are_built_from_the_pip_data_after_isolation(feature, root, child_env):
    _skip_without_loopback(feature)
    got = run_canary(_spec(root, feature), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == EXPECTED


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_a_different_search_path_in_the_environment_changes_nothing(
        feature, root, child_env, tmp_path):
    _skip_without_loopback(feature)
    env = dict(child_env, PYTHONTZPATH=str(_wrong_zone_dir(tmp_path)))
    got = run_canary(_spec(root, feature), env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == EXPECTED


def test_the_search_path_is_emptied_before_the_preload_runs(tmp_path, capsys):
    seen = []

    class Stop(Exception):
        pass

    def preload():
        seen.append(tuple(zoneinfo.TZPATH))
        raise Stop

    zoneinfo.reset_tzpath(to=["/usr/share/zoneinfo"])
    assert startup.run(object(), OpsConfig(tmp_path, tmp_path, None), lambda isolated: 0, preload=preload) \
        == startup.RC_RUNTIME_IMPORT_FAILED
    assert seen == [()]


@pytest.mark.parametrize("feature", FEATURES)
def test_every_preload_imports_the_tz_data_before_isolation(feature, monkeypatch):
    import sys
    for name in ("tzdata", "importlib.resources"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        cli.PRELOADS[feature]()
    except ModuleNotFoundError as error:  # この環境に無い機能の library は対象外
        assert error.name not in ("tzdata", "importlib.resources")
    assert "tzdata" in sys.modules and "importlib.resources" in sys.modules


def test_the_landlock_runtime_rules_do_not_name_any_tz_data_path():
    paths = {rule.path for rule in startup.runtime_rules()}
    config = sysconfig.get_paths()
    allowed = {os.path.realpath(config[k]) for k in ("stdlib", "platstdlib", "purelib", "platlib")}
    allowed |= {os.path.realpath(__import__("sys").prefix),
                os.path.realpath(os.path.dirname(__import__("afx_ops").__file__)),
                os.path.realpath(__import__("sys").executable), "/dev/urandom"}
    assert paths == allowed
    assert not any("zoneinfo" in path or "tzdata" in path for path in paths)

"""表示 tz の小さな読み手・名前の形の検査・書き込み側の正規化。"""
from __future__ import annotations

import pytest

from agentic_fx import config as config_module
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.display_tz import is_region_name, read_display_timezone, region_name_warning


def _write(tmp_path, text):
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(("text", "expected"), [
    ("display_timezone: Asia/Tokyo\n", "Asia/Tokyo"),
    ("display_timezone: America/New_York  # コメント\npairs: [USDJPY]\n", "America/New_York"),
    ("display_timezone: UTC\n", "UTC"),
    ("display_timezone: Not/AZone\n", "UTC"),
    ("display_timezone: ''\n", "UTC"),
    ("display_timezone: 9\n", "UTC"),
    ("display_timezone: [a, b]\n", "UTC"),
    ("pairs: [USDJPY]\n", "UTC"),
    ("- just\n- a list\n", "UTC"),
    ("pairs: [[[not valid\n", "UTC"),
    ("", "UTC"),
])
def test_the_reader_takes_only_display_timezone_and_never_raises(tmp_path, text, expected):
    assert read_display_timezone(_write(tmp_path, text)) == expected


def test_a_missing_or_unreadable_file_is_utc(tmp_path):
    assert read_display_timezone(tmp_path / "absent.yaml") == "UTC"
    (tmp_path / "dir.yaml").mkdir()
    assert read_display_timezone(tmp_path / "dir.yaml") == "UTC"
    binary = tmp_path / "bin.yaml"
    binary.write_bytes(b"\xff\xfe\x00display_timezone: Asia/Tokyo")
    assert read_display_timezone(binary) == "UTC"


def test_the_reader_neither_loads_settings_nor_the_env_file(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("load_settings / load_env_file must not be used")

    monkeypatch.setattr(config_module, "load_settings", fail)
    monkeypatch.setattr(config_module, "load_env_file", fail)
    monkeypatch.setattr(config_module, "load_settings_text", fail)
    assert read_display_timezone(_write(tmp_path, "display_timezone: Asia/Tokyo\n")) == "Asia/Tokyo"


@pytest.mark.parametrize(("name", "ok"), [
    ("Asia/Tokyo", True), ("America/New_York", True), ("Europe/London", True), ("UTC", True),
    ("America/Argentina/Buenos_Aires", True),
    ("EST", False), ("EST5EDT", False), ("Japan", False), ("JST", False),
    ("Etc/GMT-9", False), ("Etc/UTC", False), ("", False), (None, False), (9, False),
])
def test_region_name_shape(name, ok):
    assert is_region_name(name) is ok


def test_the_warning_names_the_value_and_the_remedy():
    assert region_name_warning("Asia/Tokyo") is None
    assert region_name_warning("UTC") is None
    text = region_name_warning("EST")
    assert "'EST'" in text and "America/New_York" in text
    assert len(region_name_warning("Etc/GMT-9" * 20)) < 200


def test_settings_with_a_compatibility_name_still_load_and_get_a_warning():
    from agentic_fx.config import load_settings_text
    from tests.status_support import REPO
    text = (REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8").replace(
        "display_timezone: Asia/Tokyo", "display_timezone: EST")
    settings = load_settings_text(text)
    assert settings.display_timezone == "EST"
    assert region_name_warning(settings.display_timezone) is not None


# ---- ActivityLog.write は文字列の category を正規化して受ける ------------------

def test_activity_write_accepts_a_plain_string_category(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write("SYSTEM", "from_a_builder", "body")
    log.write(Category.NEWS, "enum_still_works", "body")
    rows = [line.split("\t") for line in (tmp_path / "activity.log").read_text().splitlines()]
    assert [row[1:3] for row in rows] == [["SYSTEM", "from_a_builder"], ["NEWS", "enum_still_works"]]


def test_activity_write_with_an_unknown_category_does_not_raise_or_write(tmp_path):
    failures = []
    log = ActivityLog(tmp_path / "activity.log", on_write_failure=failures.append)
    log.write("NOT_A_CATEGORY", "event", "body")
    assert not (tmp_path / "activity.log").exists() or (tmp_path / "activity.log").read_text() == ""
    assert len(failures) == 1

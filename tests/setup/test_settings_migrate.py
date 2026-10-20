from pathlib import Path

import yaml

from agentic_fx.setup.settings_migrate import (
    CURRENT_SCHEMA_VERSION, _migrate_two_to_three, _migrate_zero_to_one, migrate)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "settings.yaml.example"
FIXTURES = ROOT / "tests" / "fixtures" / "settings_examples"
PLUGIN_LOCK_LINE = ("  plugin_lock_timeout_sec: 2      "
                    "# signal producer / OPEN の plugin name lock 待機上限 (秒)\n")
RECONCILE_LOCK_LINE = ("  reconcile_lock_timeout_sec: 30  "
                       "# 起動時 deploy reconcile 全体の plugin name lock 待機上限 (秒)\n")


def without_lock_lines(text: str) -> str:
    return text.replace(PLUGIN_LOCK_LINE, "", 1).replace(RECONCILE_LOCK_LINE, "", 1)


def test_version_zero_migration_inserts_only_schema_version_line():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok
    assert result.text is not None
    assert without_lock_lines(result.text).replace(
        "schema_version: 4  # settings の版。`afx init` が移行する\n", "",
    ) == old


def test_version_zero_migration_inserts_the_exact_line_before_display_timezone():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")
    result = migrate(old)
    assert result.ok
    assert result.text is not None
    assert result.text.index("schema_version: 4  # settings の版。`afx init` が移行する\n") < \
        result.text.index("display_timezone: Asia/Tokyo")


def test_version_zero_to_one_migration_remains_available_for_the_chain():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")

    result = _migrate_zero_to_one(old)

    assert result.ok
    assert result.text is not None
    assert result.text.replace(
        "schema_version: 1  # settings の版。`afx init` が移行する\n", "",
    ) == old


def test_newer_schema_is_left_unchanged_with_update_guidance():
    text = EXAMPLE.read_text(encoding="utf-8").replace("schema_version: 4", "schema_version: 5")

    result = migrate(text)

    assert not result.ok
    assert result.text is None
    assert "更新" in result.reason


def test_migration_rejects_unknown_top_level_shape_without_editing():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8").replace(
        "display_timezone: Asia/Tokyo", "display_timezone: >\n  Asia/Tokyo",
    )

    result = migrate(old)

    assert not result.ok
    assert result.text is None
    assert "手で編集してください" in result.reason


def test_every_schema_fixture_exists_and_migrates_without_changing_existing_lines():
    expected = {f"v{version}.yaml" for version in range(CURRENT_SCHEMA_VERSION + 1)}
    assert {path.name for path in FIXTURES.glob("v*.yaml")} == expected

    for fixture in sorted(FIXTURES.glob("v*.yaml")):
        original = fixture.read_text(encoding="utf-8")
        result = migrate(original)
        assert result.ok, result.reason
        assert result.text is not None
        if fixture.name == "v0.yaml":
            assert without_lock_lines(result.text).replace(
                "schema_version: 4  # settings の版。`afx init` が移行する\n", "",
            ) == original
        elif fixture.name == "v1.yaml":
            assert without_lock_lines(result.text) == original.replace(
                "schema_version: 1", "schema_version: 4", 1)
        elif fixture.name == "v2.yaml":
            assert result.text == (FIXTURES / "v3.yaml").read_text(encoding="utf-8").replace(
                "schema_version: 3", "schema_version: 4", 1)
        elif fixture.name == "v3.yaml":
            assert result.text == original.replace("schema_version: 3", "schema_version: 4", 1)
        elif fixture.name == "v4.yaml":
            assert result.text == original


def test_fixtures_are_tied_to_current_example_bytes_and_schema_version():
    example = EXAMPLE.read_text(encoding="utf-8")
    current = yaml.safe_load(example)["schema_version"]
    assert CURRENT_SCHEMA_VERSION == current
    assert (FIXTURES / f"v{current}.yaml").read_bytes() == EXAMPLE.read_bytes()


def test_version_one_migration_changes_only_the_schema_version_line():
    old = (FIXTURES / "v1.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok
    assert result.text is not None
    assert without_lock_lines(result.text).replace(
        "schema_version: 4", "schema_version: 1", 1) == old
    assert "enabled: false              # 改善 worker" in result.text


def test_version_zero_migrates_through_version_one_to_version_two():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok
    expected = (FIXTURES / "v1.yaml").read_text(encoding="utf-8").replace(
        "schema_version: 1", "schema_version: 4", 1)
    assert without_lock_lines(result.text) == expected
    assert PLUGIN_LOCK_LINE in result.text


def test_version_two_migrates_to_the_current_example_by_adding_the_lock_timeout():
    old = (FIXTURES / "v2.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok
    assert result.text == (FIXTURES / "v3.yaml").read_text(encoding="utf-8").replace(
        "schema_version: 3", "schema_version: 4", 1)
    assert yaml.safe_load(result.text)["schema_version"] == 4


def test_version_two_migration_keeps_an_existing_lock_timeout_value():
    old = (FIXTURES / "v2.yaml").read_text(encoding="utf-8").replace(
        "  snapshot_max_age_sec: 10 ", "  plugin_lock_timeout_sec: 7\n  snapshot_max_age_sec: 10 ", 1)

    result = migrate(old)

    assert result.ok
    assert result.text == old.replace("schema_version: 2", "schema_version: 4", 1).replace(
        "(lock内再取得しない)\n", "(lock内再取得しない)\n" + RECONCILE_LOCK_LINE, 1)


def test_version_two_migration_without_a_worker_section_changes_only_the_marker():
    lines = (FIXTURES / "v2.yaml").read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("worker:"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i][:1] not in (" ", "\t", "\n", "#"))
    old = "".join(lines[:start] + lines[end:])

    result = migrate(old)

    assert result.ok, result.reason
    assert result.text == old.replace("schema_version: 2", "schema_version: 4", 1)


def test_version_two_migration_without_trailing_newline_keeps_lines_separate():
    lines = (FIXTURES / "v2.yaml").read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("worker:"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i][:1] not in (" ", "\t", "\n", "#"))
    block = lines[start:end]
    while block[-1].strip() == "" or block[-1].lstrip().startswith("#"):
        block.pop()
    old = ("".join(lines[:start] + lines[end:]) + "".join(block)).rstrip("\n")
    assert not old.endswith("\n")

    result = migrate(old)

    assert result.ok, result.reason
    assert result.text.endswith("\n" + PLUGIN_LOCK_LINE + RECONCILE_LOCK_LINE)
    assert yaml.safe_load(result.text)["worker"]["plugin_lock_timeout_sec"] == 2
    assert yaml.safe_load(result.text)["worker"]["reconcile_lock_timeout_sec"] == 30


def test_version_two_marker_does_not_match_a_longer_number():
    result = _migrate_two_to_three("schema_version: 20\nworker:\n  x: 1\n")

    assert not result.ok


def test_version_four_is_a_noop():
    current = (FIXTURES / "v4.yaml").read_text(encoding="utf-8")

    result = migrate(current)

    assert result.ok
    assert result.text == current


def test_ac26_version_three_migrates_by_changing_only_the_marker():
    old = (FIXTURES / "v3.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok, result.reason
    assert result.text == old.replace("schema_version: 3", "schema_version: 4", 1)
    assert "notify:" not in result.text
    # 節が無くても既定 true で読める
    from agentic_fx.config import Settings
    assert Settings.model_validate(yaml.safe_load(result.text)).notify.trade_events is True


def test_ac26_old_yaml_without_notify_section_reads_with_default_true():
    from agentic_fx.config import Settings
    for name in ("v0", "v1", "v2", "v3"):
        raw = yaml.safe_load((FIXTURES / f"{name}.yaml").read_text(encoding="utf-8"))
        assert "notify" not in raw
        assert Settings.model_validate(raw).notify.trade_events is True


def test_ac26_example_is_v4_and_validates_with_notify_section():
    from agentic_fx.config import Settings
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 4 and raw["notify"] == {"trade_events": True}
    assert Settings.model_validate(raw).notify.trade_events is True


def test_ac26_notify_trade_events_must_be_a_real_boolean():
    import pytest
    from pydantic import ValidationError
    from agentic_fx.config import Settings
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for bad in ("yes", "false", 1, 0, None):
        raw["notify"] = {"trade_events": bad}
        with pytest.raises(ValidationError):
            Settings.model_validate(raw)
    raw["notify"] = {"trade_events": False, "extra": 1}
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)

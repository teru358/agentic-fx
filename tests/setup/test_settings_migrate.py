from pathlib import Path

import yaml

from agentic_fx.setup.settings_migrate import CURRENT_SCHEMA_VERSION, _migrate_zero_to_one, migrate


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "settings.yaml.example"
FIXTURES = ROOT / "tests" / "fixtures" / "settings_examples"


def test_version_zero_migration_inserts_only_schema_version_line():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok
    assert result.text is not None
    assert result.text.replace(
        "schema_version: 2  # settings の版。`afx init` が移行する\n", "",
    ) == old


def test_version_zero_migration_inserts_the_exact_line_before_display_timezone():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")
    result = migrate(old)
    assert result.ok
    assert result.text is not None
    assert result.text.index("schema_version: 2  # settings の版。`afx init` が移行する\n") < \
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
    text = EXAMPLE.read_text(encoding="utf-8").replace("schema_version: 2", "schema_version: 3")

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
            assert result.text.replace(
                "schema_version: 2  # settings の版。`afx init` が移行する\n", "",
            ) == original
        elif fixture.name == "v1.yaml":
            assert result.text == original.replace("schema_version: 1", "schema_version: 2", 1)
        elif fixture.name == "v2.yaml":
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
    assert result.text.replace("schema_version: 2", "schema_version: 1", 1) == old
    assert "enabled: false              # 改善 worker" in result.text


def test_version_zero_migrates_through_version_one_to_version_two():
    old = (FIXTURES / "v0.yaml").read_text(encoding="utf-8")

    result = migrate(old)

    assert result.ok
    expected = (FIXTURES / "v1.yaml").read_text(encoding="utf-8").replace(
        "schema_version: 1", "schema_version: 2", 1)
    assert result.text == expected


def test_version_two_is_a_noop():
    current = (FIXTURES / "v2.yaml").read_text(encoding="utf-8")

    result = migrate(current)

    assert result.ok
    assert result.text == current

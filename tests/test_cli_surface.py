"""The CLI surface metadata stays inert until the help integration task."""
from __future__ import annotations

import subprocess
import sys

import pytest


def test_command_records_cover_the_seven_groups_and_remain_hidden():
    from agentic_fx import cli_surface

    assert tuple(group.value for group in cli_surface.Group) == (
        "導入・接続管理", "稼働観測・実行追跡", "市場入力を整える", "調査・評価する",
        "問い合わせ・改善運用", "Plugin を採否・配備する", "保護状態を回復する")
    assert {record.group for record in cli_surface.COMMANDS if record.classified} == set(
        cli_surface.Group)
    assert all(not record.exposed for record in cli_surface.COMMANDS)
    assert all(record.implemented for record in cli_surface.COMMANDS if record.exposed)
    assert all(not record.exposed for record in cli_surface.COMMANDS if record.planned)


def test_compatibility_expiry_uses_integer_version_tuples():
    from agentic_fx import cli_surface

    assert cli_surface.version_tuple("0.10.0") > cli_surface.version_tuple("0.3.0")
    assert {record.remove_in for record in cli_surface.COMPATIBILITY_RECORDS} == {"0.3.0"}
    cli_surface.validate_compatibility_versions("0.2.999")
    with pytest.raises(ValueError, match="expired compatibility record"):
        cli_surface.validate_compatibility_versions("0.3.0")


def test_shell_stop_is_an_unclassified_change_record():
    from agentic_fx import cli_surface

    stop = next(record for record in cli_surface.COMMANDS if record.command == "stop")
    assert (stop.classified, stop.shell, stop.label, stop.shell_usage) == (
        False, True, cli_surface.Label.CHANGE, "stop")


def test_cli_surface_import_has_no_agentic_fx_import_closure():
    code = (
        "import sys, agentic_fx.cli_surface; "
        "loaded = sorted(name for name in sys.modules "
        "if name == 'agentic_fx' or name.startswith('agentic_fx.')); "
        "print(','.join(loaded)); "
        "raise SystemExit(0 if loaded == ['agentic_fx', 'agentic_fx.cli_surface'] else 1)"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr

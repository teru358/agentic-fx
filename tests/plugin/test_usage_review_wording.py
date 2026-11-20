"""棚卸しの語の表: CLI と web (afx-ops の写し) が daemon の表と同じ文言を持つ。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from agentic_fx.backtest import plugin_deploy_cli
from agentic_fx.plugin import usage_review

PLUGIN_TEXT = Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops" / "plugin_text.py"


def load_web_text():
    spec = importlib.util.spec_from_file_location("afx_ops_plugin_text_copy", PLUGIN_TEXT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_wording_equals_the_daemon_wording():
    web = load_web_text()
    assert web.USAGE_REASON == usage_review.REASON_TEXT
    assert web.USAGE_INVENTORY_REASON == usage_review.INVENTORY_REASON_TEXT
    assert web.USAGE_EXCLUDED == dict(usage_review.EXCLUDED_TEXT)


def test_the_cli_uses_the_daemon_tables_without_its_own_copy():
    assert plugin_deploy_cli.usage_review.REASON_TEXT is usage_review.REASON_TEXT
    assert not any(name in vars(plugin_deploy_cli) for name in (
        "_UNKNOWN_REASON_TEXT", "_INVENTORY_REASON_TEXT", "_EXCLUDED_TEXT"))


def test_every_reason_the_daemon_can_give_has_a_label():
    reasons = {value for name, value in vars(usage_review).items()
               if name.startswith("REASON_") and isinstance(value, str)}
    assert reasons <= set(usage_review.REASON_TEXT)

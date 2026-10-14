from __future__ import annotations

import pytest

from agentic_fx.core.idea_check import idea_looks_unintended, normalize_idea_display


def test_normalize_converts_newlines_and_drops_control_characters():
    assert normalize_idea_display("1行目\n2行目") == ("1行目 2行目", 0)
    assert normalize_idea_display("  ​hello world​  ") == ("hello world", 2)


@pytest.mark.parametrize("text,flag", [
    ("<idea>", True), ("[idea]", True), ("abc", True), ("abcd", False),
    ("<a> and <b>", True), ("ボラティリティの収縮後に入る戦略", False)])
def test_unintended_detects_placeholders_and_short_text(text, flag):
    assert idea_looks_unintended(text) is flag

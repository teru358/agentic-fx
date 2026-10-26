"""一時指示の節: プロンプトの差し込み位置とバイト一致。"""
from __future__ import annotations

import pytest

from agentic_fx.core import improve_repeat as ir
from agentic_fx.loops import improve_loop as il
from agentic_fx.loops.improve_context import mission_template
from tests.loops.test_improve_loop_prepare import (
    SETTINGS, _FakeRunContext, _sample_ctx_data)


def _loop():
    loop = il.ImproveLoop.__new__(il.ImproveLoop)
    loop._settings = SETTINGS
    return loop


def _render(tmp_path, **kw):
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    return _loop()._render_improve_mission_prompt(_sample_ctx_data(), ctx=ctx, **kw)


def test_template_places_the_section_between_policy_and_references():
    assert "{user_policy_tail}{oneoff_comment_section}\n\n## 参照" in mission_template()


def test_without_a_comment_the_prompt_is_byte_identical_to_the_previous_form(
        tmp_path, monkeypatch):
    current = _render(tmp_path)
    previous_template = mission_template().replace("{oneoff_comment_section}", "")
    assert "{user_policy_tail}\n\n## 参照" in previous_template
    monkeypatch.setattr(il, "mission_template", lambda: previous_template)
    assert _render(tmp_path) == current
    assert "今回だけの指示" not in current


def test_comment_adds_exactly_one_section_before_references(tmp_path):
    base = _render(tmp_path)
    text = _render(tmp_path, oneoff_comment="USDJPY を見て")
    assert text.count("## 今回だけの指示") == 1
    assert text.index("## ユーザー方針") < text.index("## 今回だけの指示") \
        < text.index("## 参照")
    assert text.replace(ir.render_oneoff_section("USDJPY を見て"), "") == base


def test_braces_in_the_comment_are_not_expanded(tmp_path):
    text = _render(tmp_path, oneoff_comment="{win_rate} と {} と {0} と {staging_dir}")
    assert "- {win_rate} と {} と {0} と {staging_dir}" in text


def test_same_comment_gives_the_same_section_for_every_mission(tmp_path):
    a = _render(tmp_path, oneoff_comment="同じ文")
    b = _render(tmp_path, oneoff_comment="同じ文")
    assert a == b
    assert a.count("- 同じ文") == 1


def test_last_rendered_prompt_never_keeps_the_comment(tmp_path):
    loop = _loop()
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    text = loop._render_improve_mission_prompt(
        _sample_ctx_data(), ctx=ctx, oneoff_comment="CANARY-ONEOFF")
    assert "CANARY-ONEOFF" in text
    assert "CANARY-ONEOFF" not in loop._last_rendered_prompt
    assert "今回だけの指示" not in loop._last_rendered_prompt

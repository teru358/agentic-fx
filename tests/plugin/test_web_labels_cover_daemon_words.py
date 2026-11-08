"""afx-ops の画面が持つ日本語の写し表は、daemon の read model が返す語を全部覆う。"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from agentic_fx.plugin import read_model, resolve

ROOT = Path(__file__).resolve().parents[2]


def _labels():
    path = ROOT / "afx-ops" / "afx_ops" / "plugin_text.py"
    spec = importlib.util.spec_from_file_location("plugin_text_copy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_word_the_read_model_can_return_has_a_label():
    text = _labels()
    assert set(read_model.KINDS) == set(text.KIND)
    assert {read_model.ENTITY_SYMLINK, read_model.ENTITY_PLAIN, read_model.ENTITY_ABSENT,
            read_model.ENTITY_UNREADABLE} == set(text.ENTITY)
    assert set(read_model.UNREADABLE_REASONS) == set(text.ENTITY_REASON)
    assert {read_model.ADMISSION_LOADED, read_model.ADMISSION_EXCLUDED,
            read_model.ADMISSION_UNAPPROVED, read_model.ADMISSION_UNKNOWN,
            read_model.ADMISSION_NONE} == set(text.ADMISSION)
    assert set(read_model.UNAPPROVED_REASONS) | {read_model.EXCLUDED_REASON} == set(
        text.ADMISSION_REASON)
    assert set(resolve.RESOLUTION_REASONS) | {resolve.STATE_MATCH, resolve.STATE_UNKNOWN} == set(
        text.DEPENDENCY)


def test_every_issue_the_read_model_raises_has_a_label():
    source = (ROOT / "src" / "agentic_fx" / "plugin" / "read_model.py").read_text()
    issued = set(re.findall(r'issues\.add\("([^"]+)"\)', source))
    issued |= {f"broken:{reason}" for reason in read_model.UNREADABLE_REASONS}
    assert issued and issued == set(_labels().ISSUE)


def test_the_statuses_origins_and_operation_kinds_have_labels():
    text = _labels()
    assert set(read_model.KNOWN_ORIGINS) == set(text.ORIGIN)
    assert {"pending", "approved", "rejected", "expired", "invalidated"} == set(
        text.APPROVAL_STATUS)
    assert {"deploy", "switch"} == set(text.OPEN_OP_SOURCE)
    assert {"disable", "rollback", "retire_plain", "revoke", "approve", "bless"} == set(
        text.OPEN_OP_KIND)
    assert {read_model.SOURCE_OK, read_model.SOURCE_MISSING,
            read_model.SOURCE_UNAVAILABLE} == set(text.SOURCE_STATE)


def test_the_resolution_reasons_are_exactly_the_words_the_resolver_raises():
    import ast
    raised: set[str] = set()
    for path in (ROOT / "src" / "agentic_fx").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None)
                    == "IndicatorResolutionError"):
                continue
            reason = node.args[1] if len(node.args) > 1 else None
            assert isinstance(reason, ast.Constant) and isinstance(reason.value, str), (
                f"{path}:{node.lineno} raises a reason the vocabulary cannot be checked for")
            raised.add(reason.value)
    assert raised == set(resolve.RESOLUTION_REASONS)

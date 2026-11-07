"""`afx plugin versions` の時刻は表示 tz で出る (保存の値は UTC のまま)。"""
from __future__ import annotations

from agentic_fx.plugin.deploy_versions import render_versions

_DATA = {
    "name": "sma",
    "live": {"kind": "missing", "artifact_hash": None, "target": None},
    "versions": [{
        "artifact_hash": "a" * 64, "content_hash": "c" * 64, "is_live": True,
        "decision": "approved", "approved_at": "2026-11-06T10:04:05+00:00",
        "approved_by": "operator", "decided_at": None, "decided_by": None,
        "dir_ok": True, "dir_error": None}, {
        "artifact_hash": "b" * 64, "content_hash": "d" * 64, "is_live": False,
        "decision": "rejected", "approved_at": None, "approved_by": None,
        "decided_at": "2026-11-05T23:30:00+00:00", "decided_by": "operator",
        "dir_ok": True, "dir_error": None}],
    "history": [{"effective_at": "2026-11-06T10:04:06+00:00", "kind": "deploy",
                 "from": None, "to": "x"}],
    "ops": {"open": [{
        "op_id": "o1", "op_kind": "deploy", "phase": "swap", "fail_reason": None,
        "updated_at": "2026-11-06T10:05:00+00:00", "notify_attempted": False,
        "revoke": False, "force": False, "subject_content_hash": None,
        "to_target": "t", "retired_path": None}], "attention": []},
    "dependents": {"unresolvable": [], "resolvable": []},
}


def _text(tz=None):
    lines = render_versions(_DATA) if tz is None else render_versions(_DATA, tz)
    return "\n".join(lines)


def test_instants_are_shown_in_the_display_zone():
    text = _text("Asia/Tokyo")
    assert "承認=2026-11-06 19:04:05 +09:00 by operator" in text
    assert "却下=2026-11-06 08:30:00 +09:00 by operator" in text
    assert "2026-11-06 19:04:06 +09:00  deploy" in text
    assert "updated_at=2026-11-06 19:05:00 +09:00" in text


def test_the_default_is_utc_with_an_explicit_offset():
    text = _text()
    assert "承認=2026-11-06 10:04:05 +00:00 by operator" in text
    assert "2026-11-06T10:04:05" not in text


def test_an_unconvertible_value_is_left_as_written():
    data = {**_DATA, "history": [{"effective_at": "not a time", "kind": "deploy",
                                   "from": None, "to": "x"}]}
    assert "not a time  deploy" in "\n".join(render_versions(data, "Asia/Tokyo"))

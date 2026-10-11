from __future__ import annotations

from agentic_fx.ops_setup.probe import run_probe


def test_probe_reads_only_a_fixed_first_stderr_line(tmp_path):
    script = tmp_path / "afx-ops"
    script.write_text("#!/bin/sh\nprintf 'afx-ops: config_invalid\\nsecret=never-show\\n' >&2\nexit 71\n")
    script.chmod(0o755)
    assert run_probe(str(script), "web") == (71, "config_invalid")


def test_probe_rejects_unknown_first_stderr_line(tmp_path):
    script = tmp_path / "afx-ops"
    script.write_text("#!/bin/sh\nprintf 'afx-ops: token=secret\\n' >&2\nexit 73\n")
    script.chmod(0o755)
    assert run_probe(str(script), "web") == (73, "feature_probe_unreadable")


def test_probe_rejects_non_utf8_stderr(tmp_path):
    script = tmp_path / "afx-ops"
    script.write_bytes(b"#!/bin/sh\nprintf '\\377' >&2\nexit 73\n")
    script.chmod(0o755)
    assert run_probe(str(script), "web") == (73, "feature_probe_unreadable")


def test_probe_timeout_is_bounded_and_fixed(tmp_path):
    script = tmp_path / "afx-ops"
    script.write_text("#!/bin/sh\nsleep 2\n")
    script.chmod(0o755)
    assert run_probe(str(script), "web", timeout=0.01) == (73, "feature_probe_timeout")

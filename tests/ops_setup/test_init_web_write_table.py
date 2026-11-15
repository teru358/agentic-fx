"""実 wizard (`afx init`) から web を有効にするときの、write と鍵の整合の表と、回復の記録による拒否。

wizard の入口は実物で、systemd と鍵 dir は fake / tmp を使う。
"""
from __future__ import annotations

import shutil

import pytest

from agentic_fx.ops.keys import key_dir
from agentic_fx.ops_setup.interactive import interactive_enable
from agentic_fx.setup.wizard import run_wizard
from test_web_write_idle_recovery import put_record, record_path
from test_web_write_switch import CONSISTENT, STATES, _idle_base, apply_state
from tests.setup.wizard_support import REPO, ScriptedWizardIO, fresh_responder, make_deps, snapshot
from web_support import CANDIDATES, ScriptIO, tree

RESPONDER = (("外から見る手段を今", "y"), ("有効にする手段", "web"), ("systemd のユーザーサービス", "y"))


def _wizard_over(bundle, tmp_path):
    root = bundle.root
    (root / "config").mkdir(exist_ok=True)
    shutil.copy(REPO / "config" / "settings.yaml.example", root / "config" / "settings.yaml.example")
    results: list = []

    def enable(root_, feature, *, lease=None, **kwargs):
        result = interactive_enable(root_, feature, lease=lease, io=ScriptIO(answers=[], tty=False),
                                    setup_factory=lambda _r: bundle.setup, probe_runner=lambda _f: (0, ""),
                                    candidates=lambda: CANDIDATES)
        results.append(result)
        return result

    deps, *_ = make_deps(tmp_path / "deps", enable=enable)
    io = ScriptedWizardIO(responder=fresh_responder(*RESPONDER))
    return run_wizard(root, io, deps), io, results


def _outside_the_wizard(bundle):
    return {**tree(key_dir(bundle.root)), **tree(bundle.xdg), "stage": bundle.stage_file.read_bytes()}


@pytest.mark.parametrize("state", STATES)
def test_the_real_wizard_applies_the_existing_consistency_rule_without_a_record(tmp_path, monkeypatch, state):
    bundle = _idle_base("disabled")(tmp_path, monkeypatch)
    apply_state(bundle, state)
    before = _outside_the_wizard(bundle)
    rc, io, results = _wizard_over(bundle, tmp_path)
    assert rc == 0, io.text
    assert len(results) == 1, io.text
    if state in CONSISTENT:
        assert results[0].rc == 0, (state, results[0])
    else:
        assert results[0].rc != 0, (state, results[0])
        assert _outside_the_wizard(bundle) == before
    assert not record_path(bundle).exists()


@pytest.mark.parametrize("state", STATES)
def test_a_recovery_record_stops_the_real_wizard_before_anything_is_enabled(tmp_path, monkeypatch, state):
    bundle = _idle_base("disabled")(tmp_path, monkeypatch)
    apply_state(bundle, state)
    put_record(bundle)
    before = _outside_the_wizard(bundle)
    root_before = snapshot(bundle.root)
    rc, io, results = _wizard_over(bundle, tmp_path)
    assert rc == 1 and results == [], io.text
    assert "write_switch_pending" in io.text
    assert _outside_the_wizard(bundle) == before
    assert not (bundle.root / "config" / "settings.yaml").exists()
    assert {k: v for k, v in snapshot(bundle.root).items() if k not in root_before} \
        .keys() <= {"data/instance.lock", "data/ops/setup.lock", "config/", "config/settings.yaml.example"}

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/impact_map.py'


def run(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), 'verify', '--page', 'demo', '--anchor', 'sample.py::f', *args], cwd=root, capture_output=True, text=True)


def test_verify_lifecycle(tmp_path):
    ticket = tmp_path / 'docs/tickets/open/TEST-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    source = tmp_path / 'sample.py'
    source.write_text('def f():\n    return 1\n')
    first = json.loads(run(tmp_path).stdout)
    assert first['status'] == 'missing_record'
    opts = ('--record', '--expected-hash', first['current_hash'], '--reason', 'initial_record', '--evidence', 'TEST-1')
    assert run(tmp_path, *opts).returncode == 0
    record = tmp_path / 'docs/impact-map/verification/demo.json'
    original = record.read_bytes()
    assert run(tmp_path, *opts).returncode == 0
    assert record.read_bytes() == original
    source.write_text('\n\ndef f():\n    return 1\n')
    assert json.loads(run(tmp_path).stdout)['status'] == 'fresh'
    source.write_text('def f():\n    return 2\n')
    assert json.loads(run(tmp_path).stdout)['status'] == 'stale'
    assert run(tmp_path, *opts).returncode != 0
    assert record.read_bytes() == original
    source.write_text('def g():\n    pass\n')
    assert json.loads(run(tmp_path).stdout)['status'] == 'anchor_missing'
    source.write_text('def f():\n    return 1\n')
    data = json.loads(original)
    data[0]['method'] = 'old'
    record.write_text(json.dumps(data))
    assert json.loads(run(tmp_path).stdout)['status'] == 'method_mismatch'


def test_reject_private_reason(tmp_path):
    (tmp_path / 'sample.py').write_text('def f(): pass\n')
    digest = json.loads(run(tmp_path).stdout)['current_hash']
    for reason in ('bad\nvalue', 'https://example.com', '/home/name', 'tmp/file'):
        assert run(tmp_path, '--record', '--expected-hash', digest, '--reason', reason, '--evidence', 'T-1').returncode != 0
    assert not (tmp_path / 'docs/impact-map/verification/demo.json').exists()


def test_verify_class_constant_lifecycle(tmp_path):
    ticket = tmp_path / 'docs/tickets/open/TEST-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    for assignment in ('VALUE = 1', 'VALUE: int = 1'):
        source = tmp_path / 'sample.py'
        source.write_text(f'class Outer:\n    class Inner:\n        {assignment}\n')
        anchor = 'sample.py::Outer.Inner.VALUE'
        def verify(*args):
            return subprocess.run([sys.executable, str(SCRIPT), 'verify', '--page', 'demo', '--anchor', anchor, *args], cwd=tmp_path, capture_output=True, text=True)
        first = json.loads(verify().stdout)
        assert first['status'] == 'missing_record' and len(first['current_hash']) == 64
        opts = ('--record', '--expected-hash', first['current_hash'], '--reason', 'initial_record', '--evidence', 'TEST-1')
        assert verify(*opts).returncode == 0
        assert json.loads(verify().stdout)['status'] == 'fresh'
        source.write_text(f'\nclass Outer:\n    class Inner:\n        {assignment.replace("1", "2")}\n')
        assert json.loads(verify().stdout)['status'] == 'stale'
        source.write_text('class Outer:\n    class Inner:\n        pass\n')
        assert json.loads(verify().stdout)['status'] == 'anchor_missing'
        (tmp_path / 'docs/impact-map/verification/demo.json').unlink()


def test_record_requires_allowed_reason_and_resolvable_evidence(tmp_path):
    (tmp_path / 'sample.py').write_text('def f(): pass\n')
    digest = json.loads(run(tmp_path).stdout)['current_hash']
    def record(reason, evidence):
        return run(tmp_path, '--record', '--expected-hash', digest, '--reason', reason, '--evidence', evidence)
    assert record('free text', 'T-1').returncode != 0
    assert record('initial_record', 'T-1').returncode != 0
    assert record('initial_record', 'tests/missing.py::test_x').returncode != 0
    assert not (tmp_path / 'docs/impact-map/verification/demo.json').exists()
    test = tmp_path / 'tests/test_sample.py'
    test.parent.mkdir()
    test.write_text('def test_x(): pass\n')
    assert record('initial_record', 'tests/test_sample.py::test_x').returncode == 0
    ticket = tmp_path / 'docs/tickets/closed/T-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    assert record('contract_unchanged', 'T-1').returncode == 0


def test_hash_preserves_docstrings_annotations_and_ignores_position(tmp_path):
    source = tmp_path / 'sample.py'
    source.write_text('def f(x: int):\n    """first"""\n    return x\n')
    digest = json.loads(run(tmp_path).stdout)['current_hash']
    source.write_text('\n\ndef f(x: int):\n    """first"""\n    return x\n')
    assert json.loads(run(tmp_path).stdout)['current_hash'] == digest
    source.write_text(source.read_text().replace('first', 'second'))
    assert json.loads(run(tmp_path).stdout)['current_hash'] != digest
    source.write_text(source.read_text().replace('second', 'first').replace('x: int', 'x: str'))
    assert json.loads(run(tmp_path).stdout)['current_hash'] != digest


def test_record_reports_fresh_and_canonical_json(tmp_path):
    ticket = tmp_path / 'docs/tickets/open/T-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    source = tmp_path / 'sample.py'
    source.write_text('def f(): return 1\n')
    digest = json.loads(run(tmp_path).stdout)['current_hash']
    opts = ('--record', '--expected-hash', digest, '--reason', 'initial_record', '--evidence', 'T-1')
    result = run(tmp_path, *opts)
    assert result.returncode == 0
    assert json.loads(result.stdout)['status'] == 'fresh'
    assert json.loads(result.stdout)['recorded_hash'] == digest
    path = tmp_path / 'docs/impact-map/verification/demo.json'
    first = path.read_bytes()
    assert first == (json.dumps(json.loads(first), sort_keys=True, indent=1) + '\n').encode()
    assert run(tmp_path, *opts).returncode == 0 and path.read_bytes() == first
    source.write_text('def f(): return 2\n')
    new_hash = json.loads(run(tmp_path).stdout)['current_hash']
    updated = run(tmp_path, '--record', '--expected-hash', new_hash, '--reason', 'initial_record', '--evidence', 'T-1')
    assert json.loads(updated.stdout)['status'] == 'fresh' and json.loads(updated.stdout)['recorded_hash'] == new_hash


def test_record_atomic_replace_failure_preserves_file(tmp_path, monkeypatch):
    import importlib.util
    import os
    import types
    spec = importlib.util.spec_from_file_location('impact_map_atomic_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'sample.py').write_text('def f(): return 1\n')
    ticket = tmp_path / 'docs/tickets/open/T-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    digest = module.anchor_digest('sample.py::f')[1]
    args = types.SimpleNamespace(page='demo', anchor='sample.py::f', record=True, expected_hash=digest, reason='initial_record', evidence='T-1')
    module.verify(args)
    path = tmp_path / 'docs/impact-map/verification/demo.json'
    original = path.read_bytes()
    (tmp_path / 'sample.py').write_text('def f(): return 2\n')
    args.expected_hash = module.anchor_digest(args.anchor)[1]
    def fail_replace(src, dst):
        assert Path(src).parent == path.parent and Path(src).read_bytes() != original
        assert Path(dst).resolve() == path and path.read_bytes() == original
        raise OSError('simulated interruption')
    monkeypatch.setattr(os, 'replace', fail_replace)
    import pytest
    with pytest.raises(OSError, match='simulated interruption'):
        module.verify(args)
    assert path.read_bytes() == original
    assert set(path.parent.iterdir()) == {path}

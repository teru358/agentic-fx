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

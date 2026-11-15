import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/impact_map.py'


def run(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), 'verify', '--page', 'demo', '--anchor', 'sample.py::f', *args], cwd=root, capture_output=True, text=True)


def test_verify_lifecycle(tmp_path):
    source = tmp_path / 'sample.py'
    source.write_text('def f():\n    return 1\n')
    first = json.loads(run(tmp_path).stdout)
    assert first['status'] == 'missing_record'
    opts = ('--record', '--expected-hash', first['current_hash'], '--reason', 'reviewed', '--evidence', 'TEST-1')
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

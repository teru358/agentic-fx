import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/impact_map.py'
ANCHOR = 'sample.py::f'


def cli(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=root, text=True, capture_output=True)


def setup(root):
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=T', '-c', 'user.email=t@x', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    (root / 'sample.py').write_text('def f():\n    return 1\n\ndef g():\n    pass\n')
    page = root / 'docs/impact-map/domains/demo.md'
    page.parent.mkdir(parents=True)
    meta = dict(id='demo', owner_role='owner', resources=[], anchors=[ANCHOR], related=[], tests=[])
    page.write_text('---\n' + json.dumps(meta) + '\n---\n<!-- impact-map:covers -->\n```json\n{"covers": []}\n```\n<!-- /impact-map:covers -->\n- **E1** `sample.py::f` → `sample.py::g`\n')
    ticket = root / 'docs/tickets/open/T-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    current = json.loads(cli(root, 'verify', '--page', 'demo', '--anchor', ANCHOR).stdout)['current_hash']
    assert cli(root, 'verify', '--page', 'demo', '--anchor', ANCHOR, '--record', '--expected-hash', current, '--reason', 'initial_record', '--evidence', 'T-1').returncode == 0
    return page


def check(root):
    result = cli(root, 'check', '--page', 'demo')
    return result.returncode, json.loads(result.stdout)


def test_check_lifecycle(tmp_path):
    setup(tmp_path)
    src = tmp_path / 'sample.py'
    assert check(tmp_path)[0] == 0
    src.write_text('\n\ndef f():\n    return 1\n\ndef g():\n    return 2\n')
    assert check(tmp_path)[0] == 0
    src.write_text('def f():\n    return 2\n\ndef g(): pass\n')
    code, result = check(tmp_path)
    assert code == 1 and result['counts']['stale'] == 1
    src.write_text('def renamed(): pass\n\ndef g(): pass\n')
    assert check(tmp_path)[0] == 1
    src.write_text('def f():\n    return 1\n\ndef renamed(): pass\n')
    code, result = check(tmp_path)
    assert code == 1 and any(x['kind'] == 'edge_endpoint' for x in result['items'])


def test_check_declarations_and_priority(tmp_path):
    page = setup(tmp_path)
    page.write_text(page.read_text().replace('"related": []', '"related": ["missing-ticket"]'))
    assert check(tmp_path)[0] == 1
    record = tmp_path / 'docs/impact-map/verification/demo.json'
    data = json.loads(record.read_text())
    data[0]['method'] = 'old'
    record.write_text(json.dumps(data))
    code, result = check(tmp_path)
    assert code == 2 and result['counts']['method_mismatch'] == 1 and result['counts']['ticket_missing'] == 1
    page.write_text('---\n{bad}\n---\n')
    assert check(tmp_path)[0] == 2


def test_covers_required_and_parse_errors(tmp_path):
    page = setup(tmp_path)
    text = page.read_text().replace('"covers": []', '"covers": [{"kind":"page","ids":["F1"],"resource":"state:x","symbol":"sample.py::missing"}]')
    page.write_text(text)
    code, result = check(tmp_path)
    assert code == 1 and any(x['kind'] == 'cover' and x['reason'] == 'anchor_missing' for x in result['items'])
    page.write_text(text.replace('"owner_role": "owner", ', ''))
    assert check(tmp_path)[1]['counts']['missing_key'] == 1
    page.write_text(text.replace('"covers": [', '"covers": invalid ['))
    assert check(tmp_path)[0] == 2
    page.write_text(text)
    (tmp_path / 'sample.py').write_text('def broken(:\n')
    assert check(tmp_path)[1]['counts']['parse_error'] >= 1


def test_git_unavailable(tmp_path):
    setup(tmp_path)
    import os
    result = subprocess.run([sys.executable, str(SCRIPT), 'check', '--page', 'demo'], cwd=tmp_path, env={**os.environ, 'PATH': ''}, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)['counts']['git_unavailable'] == 1


def test_conditional_nested_symbol(tmp_path):
    page = setup(tmp_path)
    (tmp_path / 'sample.py').write_text('def f():\n    return 1\n\ndef g():\n    if True:\n        def inner():\n            pass\n')
    page.write_text(page.read_text().replace('sample.py::g`', 'sample.py::g.inner`'))
    assert check(tmp_path)[0] == 0


def test_check_class_constant_lifecycle(tmp_path):
    page = setup(tmp_path)
    src = tmp_path / 'sample.py'
    src.write_text('def f(): return 1\ndef g(): pass\nclass Example:\n    VALUE: int = 1\n')
    anchor = 'sample.py::Example.VALUE'
    text = page.read_text().replace('"anchors": ["sample.py::f"]', '"anchors": ["sample.py::f", "' + anchor + '"]')
    page.write_text(text)
    assert check(tmp_path)[0] == 0
    result = cli(tmp_path, 'verify', '--page', 'demo', '--anchor', anchor)
    digest = json.loads(result.stdout)['current_hash']
    assert cli(tmp_path, 'verify', '--page', 'demo', '--anchor', anchor, '--record', '--expected-hash', digest, '--reason', 'initial_record', '--evidence', 'T-1').returncode == 0
    src.write_text(src.read_text().replace('VALUE: int = 1', 'VALUE: int = 2'))
    code, result = check(tmp_path)
    assert code == 1 and result['counts']['stale'] == 1
    src.write_text(src.read_text().replace('VALUE: int = 2', 'pass'))
    code, result = check(tmp_path)
    assert code == 1 and any(item['anchor'] == anchor and item['reason'] == 'anchor_missing' for item in result['items'])


def test_front_matter_schema_and_test_existence(tmp_path):
    page = setup(tmp_path)
    original = page.read_text()
    for old, new in [('"id": "demo"', '"id": "Bad_ID"'), ('"owner_role": "owner"', '"owner_role": 1'), ('"resources": []', '"resources": ["other:x"]'), ('"resources": []', '"resources": ["event:"]'), ('"tests": []', '"tests": 123'), ('"related": []', '"related": [1]'), ('"anchors": ["sample.py::f"]', '"anchors": "bad"')]:
        page.write_text(original.replace(old, new))
        code, result = check(tmp_path)
        assert code == 2 and result['counts']['invalid_front_matter'] == 1
    page.write_text(original.replace('"tests": []', '"tests": ["tests/missing.py::test_x"]'))
    code, result = check(tmp_path)
    assert code == 1 and result['counts']['test_missing'] == 1
    test = tmp_path / 'tests/missing.py'
    test.parent.mkdir()
    test.write_text('')
    assert check(tmp_path)[0] == 0


def test_check_public_rule_page_and_record(tmp_path):
    page = setup(tmp_path)
    original = page.read_text()
    for value in ('tmp/private', '/home/user/file', '.claude/memory/file', 'memory/secret', 'https://example.org'):
        page.write_text(original + '\n[private](' + value + ')\n')
        code, result = check(tmp_path)
        assert code == 1 and result['counts']['public_rule_violation'] >= 1
    page.write_text(original)
    record = tmp_path / 'docs/impact-map/verification/demo.json'
    data = json.loads(record.read_text())
    data[0]['reason'] = 'bad\nreason'
    record.write_text(json.dumps(data))
    code, result = check(tmp_path)
    assert code == 1 and result['counts']['public_rule_violation'] >= 1

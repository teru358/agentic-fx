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
    current = json.loads(cli(root, 'verify', '--page', 'demo', '--anchor', ANCHOR).stdout)['current_hash']
    assert cli(root, 'verify', '--page', 'demo', '--anchor', ANCHOR, '--record', '--expected-hash', current, '--reason', 'reviewed', '--evidence', 'T-1').returncode == 0
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

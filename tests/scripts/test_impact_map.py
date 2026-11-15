import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/impact_map.py'


def test_extract_does_not_execute_and_records_sites(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'sample.py').write_text("raise RuntimeError('executed')\nclass C:\n def f(self):\n  x.target\n  call(target=1)\n  return 'target'\n")
    page = tmp_path / 'page.md'
    page.write_text('---\n{"resources":["state:a.target"]}\n---\n')
    out = tmp_path / 'out'
    subprocess.run([sys.executable, str(SCRIPT), 'extract', '--domain', 'x', '--page', str(page), '--root', str(root), '--out', str(out)], check=True, cwd=tmp_path)
    records = json.loads((out / 'index.json').read_text())['records']
    assert {(r['symbol'], r['match_kind']) for r in records} == {('C.f', 'string'), ('C.f', 'attribute'), ('C.f', 'keyword')}


def run_extract(tmp_path, resources):
    page = tmp_path / 'page.md'
    page.write_text('---\n' + json.dumps({'resources': resources}) + '\n---\n')
    out = tmp_path / 'out'
    result = subprocess.run([sys.executable, str(SCRIPT), 'extract', '--domain', 'x', '--page', str(page), '--root', 'source', '--out', str(out)], cwd=tmp_path)
    return result, (out / 'index.json').read_bytes()


def test_canonical_deterministic_output(tmp_path):
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source' / 'a.py').write_text("x = 'a'\ny = 'b'\n")
    _, first = run_extract(tmp_path, ['event:b', 'event:a'])
    _, second = run_extract(tmp_path, ['event:b', 'event:a'])
    assert first == second
    assert first == (json.dumps(json.loads(first), sort_keys=True, separators=(',', ':')) + '\n').encode()
    assert [r['resource'] for r in json.loads(first)['records']] == ['event:a', 'event:b']


def test_excludes_private_and_external_inputs(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    for name in ('data', 'tmp', '.venv', 'logs', 'plugins', '.git'):
        (root / name).mkdir()
        (root / name / 'secret.py').write_text("'target'")
    (root / '.env').write_text("'target'")
    outside = tmp_path / 'outside.py'
    outside.write_text("'target'")
    (root / 'link.py').symlink_to(outside)
    _, raw = run_extract(tmp_path, ['event:target'])
    index = json.loads(raw)
    assert index['records'] == []
    assert index['header']['inputs'] == 0
    assert {'path': 'source/link.py', 'reason': 'symlink_outside_root'} in index['header']['diagnostics']


def test_ambiguous_literals_remain_candidates(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a.py').write_text("def a(x):\n return x.kill_switch_latched\ndef b():\n return 'kill_switch_latched'\n")
    _, raw = run_extract(tmp_path, ['state:app_state.kill_switch_latched', 'activity:kill_switch_latched', 'config:risk.kill_switch_latched', 'event:prefix.kill_switch_latched'])
    records = json.loads(raw)['records']
    assert {(r['resource'], r['symbol']) for r in records} == {(r, s) for r in ('state:app_state.kill_switch_latched', 'activity:kill_switch_latched', 'config:risk.kill_switch_latched') for s in ('a', 'b')}
    assert all(set(r) == {'resource', 'path', 'symbol', 'package', 'match_kind', 'reference_line'} for r in records)


def test_nested_scope_and_parse_failure(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'good.py').write_text("'target'\ndef outer():\n def inner():\n  return 'target'\n return inner\n")
    (root / 'bad.py').write_text("def bad(:\n")
    result, raw = run_extract(tmp_path, ['event:target'])
    index = json.loads(raw)
    assert result.returncode != 0
    assert {r['symbol'] for r in index['records']} == {'<module>', 'outer.inner'}
    assert index['header']['diagnostics'] == [{'path': 'source/bad.py', 'reason': 'parse_error'}]


def test_git_inputs_only(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'tracked.py').write_text("'target'\n")
    (root / 'untracked.py').write_text("'target'\n")
    subprocess.run(['git', 'add', 'source/tracked.py'], cwd=tmp_path, check=True)
    _, raw = run_extract(tmp_path, ['event:target'])
    index = json.loads(raw)
    assert index['header']['inputs'] == 1
    assert [r['path'] for r in index['records']] == ['source/tracked.py']


def test_minimum_line_and_package(tmp_path):
    root = tmp_path / 'source' / 'core'
    root.mkdir(parents=True)
    (root / 'a.py').write_text("def f(x):\n x.target\n x.target\n")
    _, raw = run_extract(tmp_path, ['state:a.target'])
    record = json.loads(raw)['records'][0]
    assert record['package'] == 'source/core'
    assert record['reference_line'] == 2


def test_missing_root_is_not_success(tmp_path):
    result, raw = run_extract(tmp_path, ['event:target'])
    assert result.returncode != 0
    assert json.loads(raw)['header']['diagnostics'] == [{'path': 'source', 'reason': 'missing_root'}]

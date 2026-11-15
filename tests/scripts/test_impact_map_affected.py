import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/impact_map.py'


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def setup(root):
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.org')
    (root / 'app.py').write_text('VALUE = 1\ndef f():\n    return 1\n\ndef g():\n    pass\n')
    page = root / 'docs/impact-map/domains/demo.md'
    page.parent.mkdir(parents=True)
    meta = {'id':'demo','resources':['event:signal'], 'anchors':['app.py::f', 'app.py::<module>.VALUE']}
    page.write_text('---\n' + json.dumps(meta) + '\n---\n<!-- impact-map:covers -->\n```json\n{"covers": []}\n```\n<!-- /impact-map:covers -->\n- **E1** `app.py::f` → `app.py::g`\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    return git(root, 'rev-parse', 'HEAD')


def run(root, base, *tail):
    p = subprocess.run([sys.executable, str(SCRIPT), 'affected', '--page', 'demo', '--base', base, *tail], cwd=root, capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


def kinds(result):
    return {item['kind'] for item in result['items']}


def test_worktree_changes(tmp_path):
    base = setup(tmp_path)
    src = tmp_path / 'app.py'
    src.write_text('\nVALUE = 1\n\ndef f():\n    return 1\n\ndef g():\n    pass\n')
    assert run(tmp_path, base, '--worktree')[1]['items'] == []
    src.write_text('VALUE = 2\ndef f():\n    return 2\n\ndef g():\n    return "signal"\n')
    code, result = run(tmp_path, base, '--worktree')
    assert code == 0 and {'symbol_changed', 'literal_candidates_changed', 'dependency_changed'} <= kinds(result)
    (tmp_path / 'new.py').write_text('def new(): pass\n')
    assert 'unmapped_new_file' in kinds(run(tmp_path, base, '--worktree')[1])
    src.write_text('VALUE = 2\ndef renamed(): pass\ndef g(): pass\n')
    assert 'unmapped_removed_dependency' in kinds(run(tmp_path, base, '--worktree')[1])


def test_revisions_and_invalid_base(tmp_path):
    base = setup(tmp_path)
    (tmp_path / 'app.py').write_text('VALUE = 1\ndef f():\n    return 2\ndef g(): pass\n')
    git(tmp_path, 'add', 'app.py')
    git(tmp_path, 'commit', '-qm', 'change')
    (tmp_path / 'app.py').write_text('broken!')
    code, result = run(tmp_path, base, '--head', 'HEAD')
    assert code == 0 and 'symbol_changed' in kinds(result)
    code, result = run(tmp_path, 'nonexistent-ref', '--head', 'HEAD')
    assert code == 2 and result['error'] == 'base_unavailable'


def test_removed_literal_and_cover(tmp_path):
    base = setup(tmp_path)
    src = tmp_path / 'app.py'
    src.write_text('VALUE = 1\ndef f(): return 1\ndef g(): return "signal"\n')
    git(tmp_path, 'add', 'app.py')
    git(tmp_path, 'commit', '-qm', 'literal')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    src.write_text('VALUE = 1\ndef f(): return 1\ndef g(): pass\n')
    result = run(tmp_path, base, '--worktree')[1]
    assert any(i['kind'] == 'literal_candidates_changed' and i['change'] == 'removed' and i['anchor'] == 'app.py::g' for i in result['items'])


def test_unavailable_head_and_invalid_source(tmp_path):
    base = setup(tmp_path)
    code, result = run(tmp_path, base, '--head', 'missing-ref')
    assert code == 2 and result['error'] == 'head_unavailable'
    (tmp_path / 'app.py').write_text('def broken(:\n')
    code, result = run(tmp_path, base, '--worktree')
    assert code == 2 and result['error'] == 'analysis_failed'


def test_cover_and_edge_symbols(tmp_path):
    base = setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    page.write_text(page.read_text().replace('"covers": []', '"covers": [{"kind":"page","ids":["F1"],"resource":"event:signal","symbol":"app.py::g"}]'))
    (tmp_path / 'app.py').write_text('VALUE = 1\ndef f(): return 1\ndef g(): return 3\n')
    code, result = run(tmp_path, base, '--worktree')
    assert code == 0 and any(i['kind'] == 'symbol_changed' and i['anchor'] == 'app.py::g' for i in result['items'])

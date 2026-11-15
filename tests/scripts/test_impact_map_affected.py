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


def test_removed_declaration_and_dependency(tmp_path):
    base = setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    page.write_text(page.read_text().replace('- **E1** `app.py::f` → `app.py::g`\n', ''))
    (tmp_path / 'app.py').write_text('VALUE = 1\ndef f(): return 1\n')
    assert any(i['kind'] == 'unmapped_removed_dependency' and i['anchor'] == 'app.py::g' for i in run(tmp_path, base, '--worktree')[1]['items'])
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'remove')
    page.write_text('invalid working tree')
    assert any(i['anchor'] == 'app.py::g' and i['kind'] == 'unmapped_removed_dependency' for i in run(tmp_path, base, '--head', 'HEAD')[1]['items'])


def test_unregistered_symbol_in_resource_file(tmp_path):
    base = setup(tmp_path)
    src = tmp_path / 'app.py'
    src.write_text(src.read_text() + '\ndef emitter():\n    emit("signal")\n')
    git(tmp_path, 'add', 'app.py')
    git(tmp_path, 'commit', '-qm', 'resource')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    src.write_text(src.read_text().replace('emit("signal")', 'emit("signal")\n    emit("new_event")'))
    assert any(i['kind'] == 'unmapped_changed_unmapped_symbol' and i['anchor'] == 'app.py::emitter' for i in run(tmp_path, base, '--worktree')[1]['items'])
    src.write_text(src.read_text().replace('    emit("new_event")\n', '').replace('return 1', 'return 2'))
    assert 'unmapped_changed_unmapped_symbol' not in kinds(run(tmp_path, base, '--worktree')[1])


def test_base_page_absent_is_partial(tmp_path):
    setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    text = page.read_text()
    page.unlink()
    git(tmp_path, 'add', '-A')
    git(tmp_path, 'commit', '-qm', 'no page')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    page.write_text(text)
    code, result = run(tmp_path, base, '--worktree')
    assert code == 0
    assert [i for i in result['items'] if i['kind'] == 'base_declarations_partial'] == [
        {'kind': 'base_declarations_partial', 'reason': 'page_absent'}]


def test_base_page_without_covers_keeps_old_declarations(tmp_path):
    setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    text = page.read_text()
    page.write_text('---\n' + json.dumps({'resources': [], 'anchors': ['app.py::g']}) + '\n---\nold page\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'old page')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    page.write_text(text.replace('- **E1** `app.py::f` → `app.py::g`\n', ''))
    (tmp_path / 'app.py').write_text('VALUE = 1\ndef f(): return 1\n')
    code, result = run(tmp_path, base, '--worktree')
    assert code == 0
    assert [i for i in result['items'] if i['kind'] == 'base_declarations_partial'] == [
        {'kind': 'base_declarations_partial', 'reason': 'covers_absent'}]
    assert any(i['kind'] == 'unmapped_removed_dependency' and i['anchor'] == 'app.py::g' for i in result['items'])


def test_broken_base_page_remains_error(tmp_path):
    setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    text = page.read_text()
    for broken in (text.replace('"covers": []', '"covers": oops'),
                   text.replace('<!-- /impact-map:covers -->', ''),
                   text.replace('"id": "demo"', '"id": broken')):
        page.write_text(broken)
        git(tmp_path, 'add', '.')
        git(tmp_path, 'commit', '-qm', 'broken page')
        base = git(tmp_path, 'rev-parse', 'HEAD')
        page.write_text(text)
        code, result = run(tmp_path, base, '--worktree')
        assert code == 2 and result['error'] == 'analysis_failed'


def test_affected_rejects_non_string_anchor(tmp_path):
    base = setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    page.write_text(page.read_text().replace('"anchors": ["app.py::f", "app.py::<module>.VALUE"]', '"anchors": [1]'))
    code, result = run(tmp_path, base, '--worktree')
    assert code == 2 and result == {'error': 'analysis_failed'}


def test_unmapped_duplicate_definitions_and_control_scopes(tmp_path):
    setup(tmp_path)
    source = tmp_path / 'app.py'
    original = '''class Example:
    @property
    def value(self): return 1
    @value.setter
    def value(self, v): pass
if True:
    def conditional(): return 1
else:
    def conditional(): return 2
try:
    def attempted(): return 1
except Exception:
    pass
with context():
    async def managed(): return 1
'''
    source.write_text(original)
    git(tmp_path, 'add', 'app.py')
    git(tmp_path, 'commit', '-qm', 'definitions')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    for old, new, anchor in [
        ('def value(self): return 1', 'def value(self): return 3', 'app.py::Example.value'),
        ('def conditional(): return 1', 'def conditional(): return 3', 'app.py::conditional'),
        ('def attempted(): return 1', 'def attempted(): return 3', 'app.py::attempted'),
        ('async def managed(): return 1', 'async def managed(): return 3', 'app.py::managed'),
    ]:
        source.write_text(original.replace(old, new))
        code, result = run(tmp_path, base, '--worktree')
        assert code == 0 and {'kind': 'unmapped_changed_unmapped_symbol', 'anchor': anchor} in result['items']


def test_class_candidate_excludes_nested_definitions_but_tracks_class_changes(tmp_path):
    setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    page.write_text(page.read_text().replace('"app.py::f"', '"app.py::Example.method"'))
    source = tmp_path / 'app.py'
    original = 'class Example(Base):\n    value = 1\n    def method(self): return 1\n    class Inner:\n        def nested(self): return 1\n'
    source.write_text(original)
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'class')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    for changed in (original.replace('def method(self): return 1', 'def method(self): return 2'),
                    original.replace('def nested(self): return 1', 'def nested(self): return 2')):
        source.write_text(changed)
        code, result = run(tmp_path, base, '--worktree')
        assert code == 0 and {'kind': 'unmapped_changed_unmapped_symbol', 'anchor': 'app.py::Example'} not in result['items']
    for changed in (original.replace('value = 1', 'value = 2'), original.replace('Example(Base)', 'Example(Other)')):
        source.write_text(changed)
        code, result = run(tmp_path, base, '--worktree')
        assert code == 0 and {'kind': 'unmapped_changed_unmapped_symbol', 'anchor': 'app.py::Example'} in result['items']


def test_removed_resource_uses_base_literal_override(tmp_path):
    setup(tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    text = page.read_text().replace('"resources": ["event:signal"]', '"resources": ["event:old"], "literals": {"event:old": ["legacy"]}')
    page.write_text(text)
    source = tmp_path / 'app.py'
    source.write_text('def emitter():\n    emit("legacy")\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'old resource')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    page.write_text(text.replace('"resources": ["event:old"], "literals": {"event:old": ["legacy"]}', '"resources": []'))
    source.write_text('def emitter():\n    pass\n')
    code, result = run(tmp_path, base, '--worktree')
    assert code == 0 and {'kind': 'literal_candidates_changed', 'anchor': 'app.py::emitter', 'resource': 'event:old', 'change': 'removed'} in result['items']

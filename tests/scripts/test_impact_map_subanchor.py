import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/impact_map.py'


def cli(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=root, capture_output=True, text=True)


def setup(root):
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'test@example.org'], check=True)
    source = root / 'app.py'
    source.write_text('def big():\n' + '    x = 1\n' * 301 + '    if x:\n        emit("one")\n        emit("two")\n    other(1)\n')
    page = root / 'docs/impact-map/domains/demo.md'
    page.parent.mkdir(parents=True)
    meta = dict(id='demo', owner_role='owner', resources=[], anchors=['app.py::big#emit@one'], related=[], tests=[])
    page.write_text('---\n' + json.dumps(meta) + '\n---\n<!-- impact-map:covers -->\n```json\n{"covers": []}\n```\n<!-- /impact-map:covers -->\n')
    ticket = root / 'docs/tickets/open/T-1.md'
    ticket.parent.mkdir(parents=True)
    ticket.write_text('')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'base'], check=True)
    return source, page


def verify(root, anchor):
    return cli(root, 'verify', '--page', 'demo', '--anchor', anchor)


def test_subanchor_verify_and_check(tmp_path):
    source, page = setup(tmp_path)
    anchor = 'app.py::big#emit@one'
    result = json.loads(verify(tmp_path, anchor).stdout)
    assert result['status'] == 'missing_record' and result['current_hash']
    opts = ('--record', '--expected-hash', result['current_hash'], '--reason', 'initial_record', '--evidence', 'T-1')
    assert verify(tmp_path, anchor + 'X').returncode == 0
    assert json.loads(verify(tmp_path, anchor + 'X').stdout)['status'] == 'anchor_missing'
    assert json.loads(verify(tmp_path, 'app.py::big#emit').stdout)['status'] == 'ambiguous_sub_anchor'
    assert cli(tmp_path, 'verify', '--page', 'demo', '--anchor', anchor, *opts).returncode == 0
    source.write_text(source.read_text().replace('other(1)', 'other(2)'))
    assert json.loads(verify(tmp_path, anchor).stdout)['status'] == 'fresh'
    source.write_text(source.read_text().replace('emit("one")', 'emit("one", 2)'))
    assert json.loads(verify(tmp_path, anchor).stdout)['status'] == 'stale'
    page.write_text(page.read_text().replace('big#emit@one', 'big#emit'))
    result = cli(tmp_path, 'check', '--page', 'demo')
    assert result.returncode == 1  # recorded anchor is stale
    assert json.loads(result.stdout)['counts']['ambiguous_sub_anchor'] == 1
    page.write_text(page.read_text().replace('big#emit', 'big'))
    (tmp_path / 'docs/impact-map/verification/demo.json').unlink()
    result = cli(tmp_path, 'check', '--page', 'demo')
    assert result.returncode == 0 and json.loads(result.stdout)['counts']['coarse_symbol'] == 1
    page.write_text(page.read_text().replace('app.py::big', 'app.py::big#emit'))
    result = cli(tmp_path, 'check', '--page', 'demo')
    assert result.returncode == 0 and json.loads(result.stdout)['counts']['ambiguous_sub_anchor'] == 1


def test_affected_subanchor_ignores_other_statement(tmp_path):
    source, page = setup(tmp_path)
    page.write_text(page.read_text().replace('\"covers\": []', '\"covers\": [{\"kind\":\"page\",\"ids\":[\"F1\"],\"resource\":\"event:x\",\"symbol\":\"app.py::big\"}]'))
    source.write_text(source.read_text().replace('other(1)', 'other(2)'))
    result = cli(tmp_path, 'affected', '--page', 'demo', '--base', 'HEAD', '--worktree')
    assert result.returncode == 0 and not any(i['kind'] == 'symbol_changed' for i in json.loads(result.stdout)['items'])
    source.write_text(source.read_text().replace('emit("one")', 'emit("one", 2)'))
    result = cli(tmp_path, 'affected', '--page', 'demo', '--base', 'HEAD', '--worktree')
    assert any(i['kind'] == 'symbol_changed' and i['anchor'] == 'app.py::big#emit@one' for i in json.loads(result.stdout)['items'])


def test_affected_reports_changed_coarse_parent(tmp_path):
    source, page = setup(tmp_path)
    def affected():
        result = cli(tmp_path, 'affected', '--page', 'demo', '--base', 'HEAD', '--worktree')
        assert result.returncode == 0
        return json.loads(result.stdout)['items']
    assert not any(i['kind'] == 'coarse_parent_changed' for i in affected())
    source.write_text(source.read_text().replace('other(1)', 'other(2)'))
    items = affected()
    assert {'kind': 'coarse_parent_changed', 'anchor': 'app.py::big'} in items
    assert not any(i['kind'] == 'symbol_changed' for i in items)

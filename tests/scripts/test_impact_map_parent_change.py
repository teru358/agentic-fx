import json
import runpy
from pathlib import Path


helpers = runpy.run_path(str(Path(__file__).with_name('test_impact_map_affected.py')))


def test_short_sub_anchor_parent_change_is_reported(tmp_path):
    helpers['setup'](tmp_path)
    page = tmp_path / 'docs/impact-map/domains/demo.md'
    meta = {'id': 'demo', 'resources': ['event:old'], 'anchors': ['app.py::f#emit@old']}
    page.write_text('---\n' + json.dumps(meta) + '\n---\n<!-- impact-map:covers -->\n```json\n{"covers": []}\n```\n<!-- /impact-map:covers -->\n')
    source = tmp_path / 'app.py'
    source.write_text('def f(flag):\n    if flag:\n        emit("old")\n')
    helpers['git'](tmp_path, 'add', '.')
    helpers['git'](tmp_path, 'commit', '-qm', 'short parent')
    base = helpers['git'](tmp_path, 'rev-parse', 'HEAD')

    code, unchanged = helpers['run'](tmp_path, base, '--worktree')
    assert code == 0 and unchanged['items'] == []

    source.write_text('def f(flag):\n    if not flag:\n        emit("old")\n    emit("new_resource")\n')
    code, changed = helpers['run'](tmp_path, base, '--worktree')
    assert code == 0
    assert changed['items'] == [{'kind': 'coarse_parent_changed', 'anchor': 'app.py::f'}]

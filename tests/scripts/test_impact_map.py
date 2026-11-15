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
    subprocess.run([sys.executable, str(SCRIPT), 'extract', '--domain', 'x', '--page', str(page), '--root', str(root), '--allow-untracked-walk', '--out', str(out)], check=True, cwd=tmp_path)
    records = json.loads((out / 'index.json').read_text())['records']
    assert {(r['symbol'], r['match_kind']) for r in records} == {('C.f', 'string_exact'), ('C.f', 'attribute'), ('C.f', 'keyword')}


def run_extract(tmp_path, resources):
    page = tmp_path / 'page.md'
    page.write_text('---\n' + json.dumps({'resources': resources}) + '\n---\n')
    out = tmp_path / 'out'
    result = subprocess.run([sys.executable, str(SCRIPT), 'extract', '--domain', 'x', '--page', str(page), '--root', 'source', '--allow-untracked-walk', '--out', str(out)], cwd=tmp_path)
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
    assert index['header']['diagnostics'] == [{'path': 'source/untracked.py', 'reason': 'untracked_runtime'}]


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


def evaluate_fixture(tmp_path, rows, records=(), covers=(), diagnostics=(), unresolved=()):
    index = tmp_path / 'index.json'
    index.write_text(json.dumps({'header': {'diagnostics': list(diagnostics), 'unsupported': []}, 'records': list(records)}))
    oracle = tmp_path / 'oracle.json'
    oracle.write_text(json.dumps({'header': {'unresolved': list(unresolved)}, 'rows': rows}))
    page = tmp_path / 'page.md'
    page.write_text('---\n{"resources":[]}\n---\n<!-- impact-map:covers -->\n```json\n' + json.dumps({'covers': list(covers)}) + '\n```\n<!-- /impact-map:covers -->\n')
    out = tmp_path / 'out'
    result = subprocess.run([sys.executable, str(SCRIPT), 'evaluate', '--domain', 'x', '--index', str(index), '--oracle', str(oracle), '--page', str(page), '--out', str(out)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    raw = (out / 'metrics.json').read_bytes()
    assert raw == (json.dumps(json.loads(raw), sort_keys=True, separators=(',', ':')) + '\n').encode()
    return json.loads(raw)


def row(symbol, resource, critical=True, cls='G_lit'):
    return {'symbol': symbol, 'resource': resource, 'critical': critical, 'class': cls, 'relation': 'reads', 'rationale': 'fixture'}


def test_evaluate_counts_ambiguous_candidates_as_fp(tmp_path):
    state, activity = 'state:app_state.kill_switch_latched', 'activity:kill_switch_latched'
    records = [{'resource': r, 'path': 'src/a.py', 'symbol': s} for r in (state, activity) for s in ('a', 'b')]
    metrics = evaluate_fixture(tmp_path, [row('src/a.py::a', state)], records)
    cell = metrics['machine']
    assert (cell['population'], cell['tp'], cell['fp'], cell['fn']) == (1, 1, 3, 0)
    assert len(cell['false_positives']) == 3


def test_evaluate_empty_population_is_unsupported(tmp_path):
    metrics = evaluate_fixture(tmp_path, [])
    assert metrics['machine']['unsupported'] and metrics['page']['unsupported']
    assert not metrics['pass_candidate']


def test_evaluate_critical_unresolved_and_manual_coverage(tmp_path):
    resource = 'event:x'
    rows = [row('src/a.py::found', resource), row('src/a.py::manual', resource, cls='G_reach'), row('src/b.py::lost', resource), row('src/c.py::unknown', resource)]
    records = [{'resource': resource, 'path': 'src/a.py', 'symbol': 'found'}]
    covers = [{'symbol': 'src/a.py::found', 'resource': resource, 'ids': ['F01'], 'kind': 'page'}, {'symbol': 'src/a.py::manual', 'resource': resource, 'ids': ['F02'], 'kind': 'manual_edge'}]
    diagnostics = [{'path': 'src/c.py', 'reason': 'parse_error'}]
    unresolved = [{'symbol': 'src/b.py::lost', 'resource': resource, 'cell': 'page', 'reason': 'fixture'}]
    metrics = evaluate_fixture(tmp_path, rows, records, covers, diagnostics, unresolved)
    machine, page = metrics['machine'], metrics['page']
    assert (machine['population'], machine['tp'], machine['fn'], machine['u'], machine['unscanned']) == (3, 1, 2, 1, 1)
    assert (page['population'], page['tp'], page['fn'], page['u']) == (4, 2, 2, 1)
    assert machine['unresolved'] == [{'resource': resource, 'symbol': 'src/c.py::unknown', 'reasons': ['parse_error']}]
    assert page['unresolved'] == [{'resource': resource, 'symbol': 'src/b.py::lost', 'reasons': ['fixture']}]
    assert machine['critical']['fn'] == 2 and machine['critical']['u'] == 1
    assert page['critical']['fn'] == 2 and page['critical']['u'] == 1
    assert not metrics['pass_candidate'] and metrics['needs_human_review']
    classification = {r['symbol']: r for r in metrics['rows']}
    assert classification['src/a.py::found']['classification'] == 'index_found'
    assert classification['src/a.py::found']['covers'][0]['ids'] == ['F01']
    assert classification['src/a.py::manual']['classification'] == 'page_manual'
    assert classification['src/b.py::lost']['classification'] == 'both_missed'


def test_evaluate_candidate_and_deduplication(tmp_path):
    resource = 'event:x'
    record = {'resource': resource, 'path': 'src/a.py', 'symbol': 'f'}
    cover = {'symbol': 'src/a.py::f', 'resource': resource, 'ids': ['F01'], 'kind': 'page'}
    metrics = evaluate_fixture(tmp_path, [row('src/a.py::f', resource), row('src/a.py::f', resource)], [record, record], [cover])
    assert metrics['pass_candidate']
    assert metrics['machine']['population'] == 1 and metrics['machine']['tp'] == 1
    assert metrics['page']['tp'] == 1 and metrics['needs_human_review']


def test_evaluate_rejects_missing_evidence(tmp_path):
    index = tmp_path / 'index.json'
    index.write_text('{"header":{"diagnostics":[],"unsupported":[]},"records":[]}')
    oracle = tmp_path / 'oracle.json'
    oracle.write_text('{"header":{},"rows":[]}')
    page = tmp_path / 'page.md'
    page.write_text('<!-- impact-map:covers -->\n```json\n{"covers":[{"symbol":"a.py::f","resource":"event:x","ids":[],"kind":"page"}]}\n```\n<!-- /impact-map:covers -->')
    result = subprocess.run([sys.executable, str(SCRIPT), 'evaluate', '--domain', 'x', '--index', str(index), '--oracle', str(oracle), '--page', str(page), '--out', str(tmp_path / 'out')], cwd=tmp_path)
    assert result.returncode != 0


def test_assignment_symbols_and_field_target(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a.py').write_text("X = 'target'\nY: str = 'target'\nclass C:\n field: bool\n attr = 'target'\n typed: str = 'target'\n")
    _, raw = run_extract(tmp_path, ['event:target', 'state:x.field'])
    assert {(r['resource'], r['symbol']) for r in json.loads(raw)['records']} == {('event:target', '<module>.X'), ('event:target', '<module>.Y'), ('event:target', 'C.attr'), ('event:target', 'C.typed'), ('state:x.field', 'C')}


def test_string_identifier_boundaries(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a.py').write_text("def exact(): return 'target'\ndef sql(): return 'FROM target;'\ndef jsonpath(): return '$.target.key'\ndef prefix(): return 'pretarget'\ndef suffix(): return 'target_1'\ndef digit(): return '1target'\n")
    _, raw = run_extract(tmp_path, ['event:target'])
    assert {(r['symbol'], r['match_kind']) for r in json.loads(raw)['records']} == {('exact', 'string_exact'), ('sql', 'string_token'), ('jsonpath', 'string_token')}


def test_explicit_marker_literals(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a.py').write_text("def f(x):\n return x._MARKER_SUFFIX\n")
    page = tmp_path / 'page.md'
    page.write_text('---\n' + json.dumps({'resources': ['marker:reset_marker'], 'literals': {'marker:reset_marker': ['_MARKER_SUFFIX']}}) + '\n---\n')
    out = tmp_path / 'out'
    subprocess.run([sys.executable, str(SCRIPT), 'extract', '--domain', 'x', '--page', str(page), '--root', str(root), '--allow-untracked-walk', '--out', str(out)], cwd=tmp_path, check=True)
    assert [(r['resource'], r['symbol']) for r in json.loads((out / 'index.json').read_text())['records']] == [('marker:reset_marker', 'f')]


def test_evaluate_nested_symbols_are_directional(tmp_path):
    resource = 'event:x'
    rows = [row('a.py::outer', resource), row('a.py::inner.child', resource), row('a.py::other', resource)]
    records = [{'resource': resource, 'path': 'a.py', 'symbol': s} for s in ('outer.nested', 'outer.second', 'inner', 'otherness')]
    metrics = evaluate_fixture(tmp_path, rows, records)
    machine = metrics['machine']
    assert (machine['tp'], machine['fn'], machine['fp']) == (1, 2, 2)
    assert machine['precision'] == 0.5
    assert {r['symbol'] for r in machine['false_positives']} == {'a.py::inner', 'a.py::otherness'}
    assert {r['symbol']: r['classification'] for r in metrics['rows']} == {'a.py::outer': 'index_found', 'a.py::inner.child': 'both_missed', 'a.py::other': 'both_missed'}


def invoke_roots(tmp_path, roots, *options, env=None):
    page = tmp_path / 'safety-page.md'
    page.write_text('---\n{"resources":["event:target"]}\n---\n')
    out = tmp_path / 'safety-out'
    args = [sys.executable, str(SCRIPT), 'extract', '--domain', 'x', '--page', str(page), '--out', str(out)]
    for root in roots:
        args += ['--root', str(root)]
    result = subprocess.run(args + list(options), cwd=tmp_path, env=env, capture_output=True, text=True)
    return result, json.loads((out / 'index.json').read_text())


def test_rejects_unsafe_roots_before_scanning(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    safe = tmp_path / 'source'
    safe.mkdir()
    (safe / 'a.py').write_text("'target'")
    subprocess.run(['git', 'add', 'source/a.py'], cwd=tmp_path, check=True)
    for name in ('data', 'logs', 'plugins', 'tmp', '.venv', '.git', 'config'):
        root = tmp_path / name / 'nested'
        root.mkdir(parents=True, exist_ok=True)
        result, index = invoke_roots(tmp_path, ['source', root])
        assert result.returncode != 0
        assert index['header']['inputs'] == 0
        assert {'path': f'{name}/nested', 'reason': 'excluded_root'} in index['header']['diagnostics']
    result, index = invoke_roots(tmp_path, ['config/../source'])
    assert result.returncode != 0
    assert index['header']['diagnostics'][0]['reason'] == 'excluded_root'
    assert index['header']['inputs'] == 0
    (tmp_path / 'alias').symlink_to(safe, target_is_directory=True)
    (safe / 'nested').mkdir()
    for root in ('alias', 'alias/nested'):
        result, index = invoke_roots(tmp_path, [root])
        assert result.returncode != 0
        assert index['header']['diagnostics'] == [{'path': root, 'reason': 'symlink_root'}]
        assert index['header']['inputs'] == 0
    result, index = invoke_roots(tmp_path, [tmp_path.parent])
    assert result.returncode != 0
    assert index['header']['diagnostics'][0]['reason'] == 'root_outside_repo'
    assert index['header']['inputs'] == 0


def test_git_failure_requires_explicit_walk(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a.py').write_text("'target'")
    result, index = invoke_roots(tmp_path, ['source'])
    assert result.returncode != 0
    assert index['header']['diagnostics'] == [{'path': 'source', 'reason': 'git_unavailable'}]
    assert index['header']['inputs'] == 0
    result, index = invoke_roots(tmp_path, ['source'], '--allow-untracked-walk')
    assert result.returncode == 0
    assert index['header']['inputs'] == 1
    assert index['records'][0]['path'] == 'source/a.py'


def test_git_tracked_list_failure_stops_even_in_repo(tmp_path, monkeypatch):
    import os
    import shutil
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a.py').write_text("'target'")
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    real_git = shutil.which('git')
    fake = bindir / 'git'
    fake.write_text(f'#!/bin/sh\nif [ "$1" = ls-files ]; then exit 1; fi\nexec "{real_git}" "$@"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
    result, index = invoke_roots(tmp_path, ['source'], env=env)
    assert result.returncode != 0
    assert index['header']['diagnostics'] == [{'path': 'source', 'reason': 'git_unavailable'}]
    assert index['header']['inputs'] == 0
    result, index = invoke_roots(tmp_path, ['source'], '--allow-untracked-walk', env=env)
    assert result.returncode == 0
    assert index['header']['inputs'] == 1


def test_repo_discovery_failure_stops_even_if_tracked_list_works(tmp_path):
    import os
    import shutil
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source' / 'a.py').write_text("'target'")
    subprocess.run(['git', 'add', 'source/a.py'], cwd=tmp_path, check=True)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    fake = bindir / 'git'
    fake.write_text(f'#!/bin/sh\nif [ "$1" = rev-parse ] && [ "$2" = --show-toplevel ]; then exit 1; fi\nexec "{shutil.which("git")}" "$@"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
    result, index = invoke_roots(tmp_path, ['source'], env=env)
    assert result.returncode != 0
    assert index['header']['diagnostics'] == [{'path': 'source', 'reason': 'git_unavailable'}]
    assert index['header']['inputs'] == 0


def test_explicit_include_reads_new_source(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source' / 'new.py').write_text("def added(): return 'target'\n")
    result, index = invoke_roots(tmp_path, ['source'], '--include', 'source/new.py')
    assert result.returncode == 0
    assert index['header']['inputs'] == 1
    assert index['header']['diagnostics'] == []
    assert [(r['path'], r['symbol']) for r in index['records']] == [('source/new.py', 'added')]


def test_untracked_diagnostic_is_repo_relative_from_subdirectory(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'project' / 'source').mkdir(parents=True)
    (tmp_path / 'project' / 'source' / 'new.py').write_text('invalid syntax !')
    result, index = invoke_roots(tmp_path / 'project', ['source'])
    assert result.returncode == 0
    assert index['header']['inputs'] == 0
    assert index['header']['diagnostics'] == [{'path': 'project/source/new.py', 'reason': 'untracked_runtime'}]


def test_include_rejects_unsafe_paths_before_reading(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'safe.py').write_text("'target'")
    (root / 'config').mkdir()
    (root / 'config' / 'private.py').write_text("'target'")
    (root / 'alias.py').symlink_to(root / 'safe.py')
    (root / 'aliasdir').symlink_to(root / 'config', target_is_directory=True)
    (root / '.hidden.py').write_text("'target'")
    (root / 'note.txt').write_text("'target'")
    (tmp_path / 'outside.py').write_text("'target'")
    nested = root / 'nested'
    nested.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=nested, check=True)
    (nested / 'nested.py').write_text("'target'")
    for name in ('source/config/private.py', 'source/alias.py', 'source/aliasdir/private.py',
                 'source/.hidden.py', 'source/note.txt', 'outside.py', 'source/missing.py', 'source/nested/nested.py'):
        result, index = invoke_roots(tmp_path, ['source'], '--include', name)
        assert result.returncode != 0, name
        assert index['header']['inputs'] == 0, name
        assert index['header']['diagnostics'], name


def test_overlapping_roots_do_not_duplicate_untracked_diagnostics_or_includes(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'source' / 'sub').mkdir(parents=True)
    (tmp_path / 'source' / 'sub' / 'new.py').write_text("def new(): return 'target'\n")
    (tmp_path / 'source' / 'sub' / 'other.py').write_text('not python !')
    result, index = invoke_roots(tmp_path, ['source', 'source/sub'], '--include', 'source/sub/new.py')
    assert result.returncode == 0
    assert index['header']['inputs'] == 1
    assert index['header']['diagnostics'] == [{'path': 'source/sub/other.py', 'reason': 'untracked_runtime'}]
    assert [(r['path'], r['symbol']) for r in index['records']] == [('source/sub/new.py', 'new')]

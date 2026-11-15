import json
import runpy
from pathlib import Path

import pytest

HERE = Path(__file__).parent
check_helpers = runpy.run_path(str(HERE / 'test_impact_map_check.py'))
affected_helpers = runpy.run_path(str(HERE / 'test_impact_map_affected.py'))


def test_record_reason_and_evidence_revalidated(tmp_path):
    check_helpers['setup'](tmp_path)
    record = tmp_path / 'docs/impact-map/verification/demo.json'
    entries = json.loads(record.read_text())
    entries[0]['reason'] = 'free text'
    record.write_text(json.dumps(entries))
    code, result = check_helpers['check'](tmp_path)
    assert code == 1 and result['counts']['public_rule_violation'] == 1
    entries[0]['reason'] = 'initial_record'
    entries[0]['evidence'] = 'tests/test_example.py::test_exists'
    record.write_text(json.dumps(entries))
    code, result = check_helpers['check'](tmp_path)
    assert code == 1 and result['counts']['test_missing'] == 1
    entries[0]['evidence'] = 'missing-ticket'
    record.write_text(json.dumps(entries))
    code, result = check_helpers['check'](tmp_path)
    assert code == 1 and result['counts']['ticket_missing'] == 1


@pytest.mark.parametrize('selector,exists', [
    ('test_exists', True), ('test_missing', False),
    ('TestSuite::test_method', True), ('TestSuite.test_method', True),
    ('TestSuite::test_missing', False), ('test_exists[case-1]', True),
    ('TestSuite::test_method[case-1]', True), ('test_missing[case-1]', False),
])
def test_test_selector_check_and_record(tmp_path, selector, exists):
    page = check_helpers['setup'](tmp_path)
    test_file = tmp_path / 'tests/test_example.py'
    test_file.parent.mkdir()
    test_file.write_text('def test_exists(): pass\nclass TestSuite:\n    async def test_method(self): pass\n')
    reference = 'tests/test_example.py::' + selector
    page.write_text(page.read_text().replace('"tests": []', '"tests": [' + json.dumps(reference) + ']'))
    code, result = check_helpers['check'](tmp_path)
    assert code == (0 if exists else 1)
    assert result['counts']['test_missing'] == (0 if exists else 1)
    digest = json.loads(check_helpers['cli'](tmp_path, 'verify', '--page', 'demo', '--anchor', 'sample.py::f').stdout)['current_hash']
    record = check_helpers['cli'](tmp_path, 'verify', '--page', 'demo', '--anchor', 'sample.py::f', '--record', '--expected-hash', digest, '--reason', 'initial_record', '--evidence', reference)
    assert (record.returncode == 0) == exists


def test_unmapped_symbol_without_known_literal(tmp_path):
    base = affected_helpers['setup'](tmp_path)
    source = tmp_path / 'app.py'
    source.write_text(source.read_text() + '\ndef unknown():\n    return "new_event"\n')
    code, result = affected_helpers['run'](tmp_path, base, '--worktree')
    assert code == 0
    assert {'kind': 'unmapped_changed_unmapped_symbol', 'anchor': 'app.py::unknown'} in result['items']


def test_record_evidence_disappears(tmp_path):
    check_helpers['setup'](tmp_path)
    test_file = tmp_path / 'tests/test_example.py'
    test_file.parent.mkdir()
    test_file.write_text('def test_exists(): pass\n')
    record = tmp_path / 'docs/impact-map/verification/demo.json'
    entries = json.loads(record.read_text())
    entries[0]['evidence'] = 'tests/test_example.py::test_exists'
    record.write_text(json.dumps(entries))
    assert check_helpers['check'](tmp_path)[0] == 0
    test_file.unlink()
    code, result = check_helpers['check'](tmp_path)
    assert code == 1 and result['counts']['test_missing'] == 1

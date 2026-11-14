"""manifest v1 の従来の判定を固定する回帰 test。

fixture 群 (`manifest_v1_corpus.py`) の期待値は、版別対応を入れる前の実装で得た結果の
逐語。v1 はこれまで受理していたものを受理し、拒否していたものを同じ理由で拒否する。
既存の呼出し (report root なし) のまま動く。"""
from __future__ import annotations

import pytest

from agentic_fx.backtest import history_migration as hm
from tests.backtest import manifest_v1_corpus as corpus

EXPECTED_VALIDATE = {'initial': [],
 'evaluated': [],
 'jst-everywhere': [],
 'naive-evaluated-at': ['#32: evaluated_at must be an aware UTC time',
                        '#92: evaluated_at must be an aware UTC time'],
 'unevaluable-fixed-reason': [],
 'unevaluable-new-v2-reason': ['#65: unevaluable needs a fixed reason',
                               '#94: unevaluable needs a fixed reason'],
 'unevaluable-dependency-changed': ['#65: unevaluable needs a fixed reason',
                                    '#94: unevaluable needs a fixed reason'],
 'pending-with-stray-fields': [],
 'deferred-with-stray-fields': [],
 'decided-non-utc': [],
 'schema-true': [],
 'schema-float': [],
 'schema-three': ['schema_version must be 1'],
 'schema-string': ['schema_version must be 1'],
 'schema-float-two': ['schema_version must be 1'],
 'created-naive': ['created_at must be an aware UTC time'],
 'created-non-utc': [],
 'extra-top': ['the manifest must have exactly the specified top-level fields'],
 'missing-top': ['the manifest must have exactly the specified top-level fields'],
 'entry-extra-key': ['#32: the entry must have exactly the specified fields'],
 'entry-missing-key': ['#32: the entry must have exactly the specified fields'],
 'not-dict': ['entries must be exactly the 15 old approvals, with no duplicate or gap'],
 'short': ['entries must hold exactly the 15 old approvals'],
 'dup': ['entries must hold exactly the 15 old approvals'],
 'wrong-id': ['entries must be exactly the 15 old approvals, with no duplicate or gap'],
 'short-hash': ['#32: artifact_hash must be the full 64-character hash'],
 'name': ['#32: name differs'],
 'nondeployed-evaluated': ['#21: a non-deployed version stays phase 1b / deferred',
                           '#21: a non-deployed version must not point at a new approval'],
 'nondeployed-pending': ['#21: a non-deployed version stays phase 1b / deferred'],
 'nondeployed-new-id': ['#21: a non-deployed version must not point at a new approval',
                        '#21: only an evaluated entry has a new approval id'],
 'nondeployed-unevaluable': ['#21: a non-deployed version stays phase 1b / deferred'],
 'decision-without-time': ['#32: a decided entry needs decided_at'],
 'unknown-decision': ['#32: human_decision is unknown'],
 'unknown-status': ['#32: result_status is unknown'],
 'pair-differs': ['ichimoku_rsi_cloud_reentry: the two old approvals of one deployed version '
                  'must record the same result'],
 'evaluated-no-ref': ['#32: evidence_ref is required'],
 'evaluated-no-time': ['#32: evaluated_at must be an aware UTC time']}

EXPECTED_VERIFY = {'evaluated|match': [],
 'evaluated|hash': ['#21: name / hashes differ from the old approval'],
 'evaluated|name': ['#32: name / hashes differ from the old approval'],
 'evaluated|missing': ['#65: the old approval does not exist'],
 'evaluated|noevidence': ['#32: the new approval has no valid history evidence',
                          '#92: the new approval has no valid history evidence',
                          '#65: the new approval has no valid history evidence',
                          '#94: the new approval has no valid history evidence',
                          '#73: the new approval has no valid history evidence',
                          '#89: the new approval has no valid history evidence',
                          '#74: the new approval has no valid history evidence',
                          '#91: the new approval has no valid history evidence'],
 'evaluated|decided': ['#32: the database already decided this request',
                       '#92: the database already decided this request',
                       '#65: the database already decided this request',
                       '#94: the database already decided this request',
                       '#73: the database already decided this request',
                       '#89: the database already decided this request',
                       '#74: the database already decided this request',
                       '#91: the database already decided this request'],
 'initial|match': [],
 'initial|hash': ['#21: name / hashes differ from the old approval'],
 'initial|name': ['#32: name / hashes differ from the old approval'],
 'initial|missing': ['#65: the old approval does not exist'],
 'initial|noevidence': [],
 'initial|decided': [],
 'jst-everywhere|match': [],
 'jst-everywhere|hash': ['#21: name / hashes differ from the old approval'],
 'jst-everywhere|name': ['#32: name / hashes differ from the old approval'],
 'jst-everywhere|missing': ['#65: the old approval does not exist'],
 'jst-everywhere|noevidence': ['#32: the new approval has no valid history evidence',
                               '#92: the new approval has no valid history evidence',
                               '#65: the new approval has no valid history evidence',
                               '#94: the new approval has no valid history evidence',
                               '#73: the new approval has no valid history evidence',
                               '#89: the new approval has no valid history evidence',
                               '#74: the new approval has no valid history evidence',
                               '#91: the new approval has no valid history evidence'],
 'jst-everywhere|decided': ['#32: the database already decided this request',
                            '#92: the database already decided this request',
                            '#65: the database already decided this request',
                            '#94: the database already decided this request',
                            '#73: the database already decided this request',
                            '#89: the database already decided this request',
                            '#74: the database already decided this request',
                            '#91: the database already decided this request'],
 'unevaluable-fixed-reason|match': [],
 'unevaluable-fixed-reason|hash': ['#21: name / hashes differ from the old approval'],
 'unevaluable-fixed-reason|name': ['#32: name / hashes differ from the old approval'],
 'unevaluable-fixed-reason|missing': ['#65: the old approval does not exist'],
 'unevaluable-fixed-reason|noevidence': [],
 'unevaluable-fixed-reason|decided': []}


@pytest.mark.parametrize("case_id", [c for c, _ in corpus.VALIDATE_CASES])
def test_v1_validate_keeps_its_previous_verdict(case_id):
    assert hm.validate_manifest(corpus.build(case_id)) == EXPECTED_VALIDATE[case_id]


@pytest.mark.parametrize("key", sorted(EXPECTED_VERIFY))
def test_v1_verify_against_db_keeps_its_previous_verdict(tmp_path, key):
    case_id, variant = key.split("|")
    manifest = corpus.build(case_id)
    conn = corpus.make_db(tmp_path, manifest, variant=variant)
    try:
        assert hm.verify_against_db(
            manifest, conn, no_bars_path=tmp_path / "none.yaml") == EXPECTED_VERIFY[key]
    finally:
        conn.close()


def test_a_v1_manifest_is_accepted_without_a_report_root():
    import inspect
    parameters = inspect.signature(hm.verify_against_db).parameters
    assert parameters["report_root"].default is None
    assert hm.validate_manifest(corpus.build("jst-everywhere")) == []
    assert hm.validate_manifest(corpus.build("pending-with-stray-fields")) == []

"""manifest 版 2: 各行の `audit_ref`、厳格な形、版 1 からの明示変換、report との照合。

厳格化は版 2 だけに課す。版 1 の従来の受理範囲は test_history_migration_v1_compat.py が固定する。"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone

import pytest

from agentic_fx.backtest import history_migration as hm
from tests.backtest import manifest_v1_corpus as corpus
from tests.history_support import ready_evidence

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
NONDEPLOYED = (21, 23, 24, 25, 39, 71, 72)


def v2_manifest():
    converted, problems = hm.convert_to_v2(corpus.base_manifest())
    assert problems == []
    return converted


def entry(manifest, approval_id):
    return corpus.entry(manifest, approval_id)


def report_for(e, *, overall="pass", reason=None, pairs=("USDJPY",), started=STAMP):
    pair_list = list(pairs)
    per_pair = lambda result: {p: {"result": result} for p in pair_list}  # noqa: E731
    floor = {s: {"result": "pass", "pairs": per_pair("pass")} for s in ("in_sample", "holdout")}
    history = {"result": "ready_verified", "evidence_check": "ok", "epoch": "none",
               "evidence": ready_evidence(source="dukascopy", base_interval="5m", pairs=pair_list,
                                          now=NOW)}
    report = {
        "report_format": 1, "trial_id": "T", "old_approval_id": e["old_approval_id"],
        "name": e["name"], "artifact_hash": e["artifact_hash"],
        "content_hash": e["content_hash"], "dependencies": [], "input_checks": {},
        "evaluation": {"started_at": started, "pairs": pair_list, "history_epoch": "none",
                       "dataset": {"source": "dukascopy", "base_interval": "5m"}},
        "axes": {"execution": {"status": "completed", "stage": "recheck"}, "floor": floor,
                 "history": history, "unexecuted": []},
        "changed_during_evaluation": [],
        "result": {"overall": overall, "reason": reason,
                   "result_status": "unevaluable" if overall == "unevaluable" else "evaluated"},
        "rows": [{"local_row_id": i, "db_run_id": None, "scope": s, "pair": p}
                 for i, (s, p) in enumerate(((s, p) for s in ("in_sample", "holdout")
                                             for p in pair_list), 1)]}
    return report


def publish(root, e, report=None, **kwargs):
    """report を root に置き、その行へ audit_ref・時刻・参照・結果を記録する。"""
    report = report or report_for(e, **kwargs)
    name = f"{e['old_approval_id']}-T.json"
    report["trial_id"] = f"{e['old_approval_id']}-T"
    (root / name).write_text(json.dumps(report))
    overall = report["result"]["overall"]
    e.update(result_status="unevaluable" if overall == "unevaluable" else "evaluated",
             audit_ref=name, evaluated_at=report["evaluation"]["started_at"],
             evidence_ref="#result",
             unevaluable_reason=report["result"]["reason"] if overall == "unevaluable" else None)
    return name


# ---- 形 ---------------------------------------------------------------------------

def test_a_converted_manifest_has_the_same_15_rows_plus_a_null_audit_ref():
    v1 = corpus.base_manifest()
    before = copy.deepcopy(v1)
    v2, problems = hm.convert_to_v2(v1)
    assert problems == [] and v1 == before            # 元は変えない
    assert v2["schema_version"] == 2 and hm.validate_manifest(v2) == []
    assert [e["audit_ref"] for e in v2["entries"]] == [None] * 15
    assert v2["created_at"] == v1["created_at"] and v2["updated_at"] == v1["updated_at"]
    for old, new in zip(v1["entries"], v2["entries"]):
        assert {k: v for k, v in new.items() if k != "audit_ref"} == old


def test_the_phase_1a_rows_keep_their_results_and_references_through_the_conversion():
    v1 = corpus.base_manifest()
    corpus.evaluate_all(v1)
    entry(v1, 32).update(human_decision="approved", decided_at=STAMP)
    v2, problems = hm.convert_to_v2(v1)
    assert problems == []
    assert entry(v2, 32)["human_decision"] == "approved"
    assert entry(v2, 32)["new_reevaluation_approval_id"] == entry(v1, 32)[
        "new_reevaluation_approval_id"]
    assert entry(v2, 92)["evidence_ref"] == "approval:200"


@pytest.mark.parametrize("case,needle", [
    ("jst-everywhere", "aware UTC"),
    ("pending-with-stray-fields", "must be null until"),
    ("deferred-with-stray-fields", "must be null until"),
    ("decided-non-utc", "aware UTC"),
    ("created-non-utc", "aware UTC"),
])
def test_a_v1_manifest_that_v2_would_reject_is_not_converted_and_the_gap_is_reported(
        case, needle):
    v1 = corpus.build(case)
    before = copy.deepcopy(v1)
    converted, problems = hm.convert_to_v2(v1)
    assert converted is None and any(needle in p for p in problems)
    assert v1 == before


def test_a_manifest_that_v1_itself_rejects_is_not_converted():
    converted, problems = hm.convert_to_v2(corpus.build("short"))
    assert converted is None and problems


@pytest.mark.parametrize("case", ["jst-everywhere", "pending-with-stray-fields",
                                  "deferred-with-stray-fields", "decided-non-utc"])
def test_what_v1_accepts_is_not_enough_for_v2(case):
    v1 = corpus.build(case)
    assert hm.validate_manifest(v1) == []
    forced = copy.deepcopy(v1)
    forced["schema_version"] = 2
    for e in forced["entries"]:
        e["audit_ref"] = None
    assert hm.validate_manifest(forced) != []


@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["entries"][0].pop("audit_ref"), "exactly the specified fields"),
    (lambda m: m["entries"][0].update(extra=1), "exactly the specified fields"),
    (lambda m: m.update(extra=1), "top-level"),
    (lambda m: m.update(created_at="2026-11-14T21:00:00+09:00"), "created_at"),
    (lambda m: m["entries"].pop(), "exactly the 15"),
    (lambda m: entry(m, 32).update(audit_ref="x.json"), "phase 1a row has no audit_ref"),
    (lambda m: entry(m, 32).update(result_status="pending", evaluated_at=STAMP),
     "must be null until"),
    (lambda m: entry(m, 32).update(result_status="evaluated", new_reevaluation_approval_id=1),
     "evaluated_at"),
    (lambda m: entry(m, 21).update(human_decision="pending"), "not_applicable"),
    (lambda m: entry(m, 21).update(decided_at=STAMP), "not_applicable"),
    (lambda m: entry(m, 21).update(new_reevaluation_approval_id=3), "new approval"),
    (lambda m: entry(m, 21).update(phase="1a"), "phase 1b"),
    (lambda m: entry(m, 21).update(result_status="evaluated"), "audit_ref is required"),
    (lambda m: entry(m, 21).update(result_status="unevaluable", unevaluable_reason="history_stale"),
     "audit_ref is required"),
    (lambda m: entry(m, 21).update(audit_ref="../escape.json"), "relative"),
    (lambda m: entry(m, 21).update(audit_ref="/abs.json"), "relative"),
    (lambda m: entry(m, 21).update(audit_ref="a//b.json"), "relative"),
    (lambda m: entry(m, 21).update(audit_ref="a.txt"), "relative"),
    (lambda m: [entry(m, i).update(
        audit_ref="same.json", result_status="evaluated", evaluated_at=STAMP,
        evidence_ref="#result") for i in (21, 23)], "same audit report"),
])
def test_a_malformed_v2_manifest_is_rejected(mutate, needle):
    manifest = v2_manifest()
    mutate(manifest)
    assert any(needle in p for p in hm.validate_manifest(manifest))


def test_evaluated_and_unevaluable_need_a_utc_time_and_a_reference_in_every_phase():
    for approval_id in (32, 21):
        manifest = v2_manifest()
        e = entry(manifest, approval_id)
        e.update(result_status="unevaluable", unevaluable_reason="history_stale",
                 audit_ref="r.json" if approval_id == 21 else None)
        assert any("evaluated_at" in p for p in hm.validate_manifest(manifest))
        e.update(evaluated_at=STAMP)
        assert any("evidence_ref" in p for p in hm.validate_manifest(manifest))
        e.update(evidence_ref="#result")
        if approval_id == 32:
            e["result_status"] = "unevaluable"
            entry(manifest, 92).update(result_status="unevaluable",
                                       unevaluable_reason="history_stale", evaluated_at=STAMP,
                                       evidence_ref="#result")
        assert hm.validate_manifest(manifest) == []
        e["evaluated_at"] = "2026-11-14T21:00:00+09:00"
        assert any("aware UTC" in p for p in hm.validate_manifest(manifest))


def test_v2_accepts_the_new_fixed_reasons_that_v1_does_not():
    for reason in ("artifact_missing", "dependency_changed", "sandbox_unavailable",
                   "history_stale", "failed"):
        manifest = v2_manifest()
        entry(manifest, 21).update(
            result_status="unevaluable", unevaluable_reason=reason, audit_ref="r.json",
            evaluated_at=STAMP, evidence_ref="#result")
        assert hm.validate_manifest(manifest) == []
    entry(manifest, 21)["unevaluable_reason"] = "free text"
    assert any("fixed reason" in p for p in hm.validate_manifest(manifest))
    assert "artifact_missing" not in hm.UNEVALUABLE_REASONS
    assert "dependency_changed" not in hm.UNEVALUABLE_REASONS


def test_the_two_old_approvals_of_one_version_still_record_the_same_result_in_v2():
    manifest = v2_manifest()
    corpus.evaluate_all(manifest)
    for e in manifest["entries"]:
        e["audit_ref"] = None
    assert hm.validate_manifest(manifest) == []
    entry(manifest, 92)["new_reevaluation_approval_id"] = 999
    assert any("same result" in p for p in hm.validate_manifest(manifest))


# ---- DB と report の照合 -------------------------------------------------------------

def _verify(tmp_path, manifest, root=None, *, variant="match"):
    conn = corpus.make_db(tmp_path, manifest, variant=variant)
    try:
        kwargs = {} if root is None else {"report_root": root}
        return hm.verify_against_db(manifest, conn, no_bars_path=tmp_path / "none.yaml", **kwargs)
    finally:
        conn.close()
        (tmp_path / f"v-{variant}.db").unlink()


def test_an_initial_v2_manifest_verifies_without_a_report_root(tmp_path):
    assert _verify(tmp_path, v2_manifest()) == []


def test_the_phase_1a_rows_are_still_checked_against_the_database_in_v2(tmp_path):
    manifest = v2_manifest()
    corpus.evaluate_all(manifest)
    for e in manifest["entries"]:
        e["audit_ref"] = None
    assert _verify(tmp_path, manifest) == []
    problems = _verify(tmp_path, manifest, variant="noevidence")
    assert any("no valid history evidence" in p for p in problems)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "reports"
    path.mkdir()
    return path


def _one(root, **kwargs):
    manifest = v2_manifest()
    e = entry(manifest, 21)
    name = publish(root, e, **kwargs)
    return manifest, e, name


def test_a_passing_report_that_matches_verifies(tmp_path, root):
    manifest, _e, _name = _one(root)
    assert hm.validate_manifest(manifest) == []
    assert _verify(tmp_path, manifest, root) == []


def test_a_failing_and_an_unevaluable_report_verify_too(tmp_path, root):
    manifest = v2_manifest()
    publish(root, entry(manifest, 21), overall="fail", reason="floor")
    publish(root, entry(manifest, 23), overall="unevaluable", reason="history_stale")
    assert _verify(tmp_path, manifest, root) == []


def test_a_reference_to_a_report_needs_the_report_root(tmp_path, root):
    manifest, _e, _name = _one(root)
    assert any("report_root is required" in p for p in _verify(tmp_path, manifest))


def test_a_missing_report_is_flagged(tmp_path, root):
    manifest, _e, name = _one(root)
    (root / name).unlink()
    assert any("report is missing" in p for p in _verify(tmp_path, manifest, root))


def test_a_symlinked_report_is_refused_even_when_it_holds_the_right_bytes(tmp_path, root):
    manifest, _e, name = _one(root)
    real = tmp_path / "elsewhere.json"
    (root / name).rename(real)
    os.symlink(real, root / name)
    assert any("not a regular file" in p for p in _verify(tmp_path, manifest, root))


def test_a_report_reached_through_a_symlinked_directory_is_refused(tmp_path, root):
    manifest = v2_manifest()
    outside = tmp_path / "outside"
    outside.mkdir()
    e = entry(manifest, 21)
    name = publish(outside, e)
    os.symlink(outside, root / "linked")
    e["audit_ref"] = f"linked/{name}"
    assert any("leaves the report root" in p for p in _verify(tmp_path, manifest, root))


def test_an_invalid_json_report_is_flagged(tmp_path, root):
    manifest, _e, name = _one(root)
    (root / name).write_text("{not json")
    assert any("not valid JSON" in p for p in _verify(tmp_path, manifest, root))


@pytest.mark.parametrize("field,value,needle", [
    ("artifact_hash", "d" * 64, "another version"),
    ("name", "other", "another version"),
    ("old_approval_id", 23, "another version"),
    ("report_format", 9, "format"),
    ("trial_id", "other", "trial id"),
])
def test_a_report_of_another_version_or_form_is_flagged(tmp_path, root, field, value, needle):
    manifest, e, name = _one(root)
    data = json.loads((root / name).read_text())
    data[field] = value
    (root / name).write_text(json.dumps(data))
    assert any(needle in p for p in _verify(tmp_path, manifest, root))


def test_the_recorded_time_status_and_reason_must_agree_with_the_report(tmp_path, root):
    manifest, e, _name = _one(root)
    e["evaluated_at"] = "2026-11-14T13:00:00+00:00"
    assert any("evaluation time differs" in p for p in _verify(tmp_path, manifest, root))
    manifest, e, _name = _one(root)
    e.update(result_status="unevaluable", unevaluable_reason="history_stale")
    assert any("result_status differs" in p for p in _verify(tmp_path, manifest, root))
    manifest = v2_manifest()
    publish(root, entry(manifest, 23), overall="unevaluable", reason="history_stale")
    entry(manifest, 23)["unevaluable_reason"] = "history_gap"
    assert any("recorded reason differs" in p for p in _verify(tmp_path, manifest, root))


def test_the_evidence_reference_must_name_a_section_of_the_report(tmp_path, root):
    manifest, e, _name = _one(root)
    e["evidence_ref"] = "#nowhere"
    assert any("evidence_ref" in p for p in _verify(tmp_path, manifest, root))


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r["axes"]["floor"]["holdout"].update(result="fail"), "holdout scope"),
    (lambda r: r["axes"]["floor"]["in_sample"]["pairs"]["USDJPY"].update(result="fail"),
     "in_sample scope"),
    (lambda r: r["axes"]["unexecuted"].append({"item": "x"}), "unexecuted"),
    (lambda r: r["axes"]["history"].update(result="failed"), "verified ready evidence"),
    (lambda r: r["axes"]["history"].update(evidence_check="invalid"), "verified ready evidence"),
    (lambda r: r["axes"]["history"].update(epoch=None), "history epoch"),
    (lambda r: r["axes"]["history"].update(evidence=None), "does not belong"),
    (lambda r: r["axes"]["history"]["evidence"].update(pairs=["EURUSD"]), "does not belong"),
    (lambda r: r.update(changed_during_evaluation=["history_epoch"]), "change during"),
    (lambda r: r["rows"].pop(), "row for every pair"),
    (lambda r: r["rows"][0].update(db_run_id=5), "database run"),
    (lambda r: r["axes"]["execution"].update(status="interrupted"), "completed run"),
    (lambda r: r["result"].update(result_status="unevaluable"), "contradicts"),
])
def test_a_pass_needs_every_scope_floor_and_a_verified_evidence(tmp_path, root, mutate, needle):
    manifest = v2_manifest()
    e = entry(manifest, 21)
    report = report_for(e)
    mutate(report)
    publish(root, e, report)
    assert any(needle in p for p in _verify(tmp_path, manifest, root))


def test_the_database_pins_of_a_non_deployed_row_are_checked(tmp_path, root):
    manifest, e, _name = _one(root)
    e["dependency_pins"] = {"fast": {"plugin": "ind", "content_hash": "a" * 64, "params": {}}}
    problems = _verify(tmp_path, manifest, root)
    assert any("dependency pins differ" in p for p in problems)


def test_the_old_approval_hashes_of_a_non_deployed_row_are_checked(tmp_path, root):
    manifest, _e, _name = _one(root)
    assert any("differ from the old approval" in p
               for p in _verify(tmp_path, manifest, root, variant="hash"))


def test_the_current_validity_of_an_evidence_is_a_separate_question(tmp_path, root):
    manifest, e, name = _one(root)
    report = json.loads((root / name).read_text())
    assert hm.evidence_currently_valid(
        report, no_bars_path=tmp_path / "none.yaml").ok
    report["axes"]["history"]["evidence"]["calendar_version"] = "older"
    (root / name).write_text(json.dumps(report))
    assert not hm.evidence_currently_valid(report, no_bars_path=tmp_path / "none.yaml").ok
    # 記録の整合は、現在の方針が変わっても歴史 report を落とさない
    assert not any("calendar" in p for p in _verify(tmp_path, manifest, root))

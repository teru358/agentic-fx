"""移行 manifest の形と受入条件 (15 行、配備中 8 行、同じ版の 2 行、非配備 7 行)。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from agentic_fx.backtest import history_migration as hm
from agentic_fx.store import approvals as approvals_store
from tests.history_support import ready_evidence
from tests.plugin.deploy_world import at
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _versions():
    out = {}
    for approval_id, name, prefix, _deployed in hm.EXPECTED_ENTRIES:
        out[approval_id] = {"artifact_hash": prefix + "a" * (64 - len(prefix)),
                            "content_hash": "c" * 64, "dependency_pins": {}}
    return out


def _manifest():
    return hm.initial_manifest(base_commit="e63f8e44", backup_id="bk-1", recorded_by="ops",
                               now=NOW, versions=_versions())


def _entry(manifest, approval_id):
    return next(e for e in manifest["entries"] if e["old_approval_id"] == approval_id)


def _evaluate_all(manifest, *, new_ids=None):
    for number, version in enumerate(("3b35b61fa798", "127825cc55df", "0b5ec2c155db",
                                      "ee670b8630f6")):
        for entry in manifest["entries"]:
            if entry["artifact_hash"].startswith(version):
                entry.update(result_status="evaluated", evaluated_at=NOW.isoformat(),
                             new_reevaluation_approval_id=(new_ids or {}).get(version, 200 + number),
                             evidence_ref="approval:200")


def test_the_initial_manifest_has_15_rows_and_marks_the_nondeployed_as_deferred():
    manifest = _manifest()
    assert len(manifest["entries"]) == 15 and hm.validate_manifest(manifest) == []
    deployed = [e for e in manifest["entries"] if e["deployed_at_inventory"]]
    assert len(deployed) == 8 and {e["phase"] for e in deployed} == {"1a"}
    rest = [e for e in manifest["entries"] if not e["deployed_at_inventory"]]
    assert len(rest) == 7 and {(e["phase"], e["result_status"]) for e in rest} == {
        ("1b", "deferred")}


def test_an_evaluated_manifest_validates_and_unevaluable_needs_a_fixed_reason():
    manifest = _manifest()
    _evaluate_all(manifest)
    assert hm.validate_manifest(manifest) == []
    for entry in manifest["entries"]:
        if entry["name"] == "volume_close_pressure":
            entry.update(result_status="unevaluable", new_reevaluation_approval_id=None,
                         unevaluable_reason="history_stale")
    assert hm.validate_manifest(manifest) == []
    _entry(manifest, 65)["unevaluable_reason"] = "free text"
    assert any("fixed reason" in p for p in hm.validate_manifest(manifest))


@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["entries"].pop(), "exactly the 15"),
    (lambda m: m["entries"].append(copy.deepcopy(m["entries"][0])), "exactly the 15"),
    (lambda m: m["entries"][0].update(old_approval_id=999), "15 old approvals"),
    (lambda m: m.update(schema_version=2), "schema_version"),
    (lambda m: m.update(created_at="2026-11-11T12:00:00"), "created_at"),
    (lambda m: m.update(extra=1), "top-level"),
    (lambda m: _entry(m, 32).update(artifact_hash="3b35b61fa798"), "full 64"),
    (lambda m: _entry(m, 32).update(name="other"), "name differs"),
    (lambda m: _entry(m, 21).update(phase="1a"), "phase 1b"),
    (lambda m: _entry(m, 21).update(result_status="pending"), "phase 1b"),
    (lambda m: _entry(m, 21).update(new_reevaluation_approval_id=5), "non-deployed"),
    (lambda m: _entry(m, 32).update(phase="1b"), "phase 1a"),
    (lambda m: _entry(m, 32).update(deployed_at_inventory=False), "inventory"),
    (lambda m: _entry(m, 32).update(result_status="done"), "result_status"),
    (lambda m: _entry(m, 32).update(human_decision="maybe"), "human_decision"),
])
def test_a_malformed_manifest_is_rejected(mutate, needle):
    manifest = _manifest()
    mutate(manifest)
    assert any(needle in problem for problem in hm.validate_manifest(manifest))


def test_an_evaluated_row_needs_an_id_a_time_and_an_evidence_reference():
    manifest = _manifest()
    _evaluate_all(manifest)
    _entry(manifest, 32).update(evaluated_at=None)
    assert any("evaluated_at" in p for p in hm.validate_manifest(manifest))
    _entry(manifest, 32).update(evaluated_at=NOW.isoformat(), evidence_ref="")
    assert any("evidence_ref" in p for p in hm.validate_manifest(manifest))
    _entry(manifest, 32).update(evidence_ref="x", new_reevaluation_approval_id=None)
    assert any("new approval id" in p for p in hm.validate_manifest(manifest))


def test_the_two_old_approvals_of_one_version_must_record_the_same_result():
    manifest = _manifest()
    _evaluate_all(manifest)
    _entry(manifest, 92).update(new_reevaluation_approval_id=999)
    assert any("same result" in p for p in hm.validate_manifest(manifest))


def test_a_decision_needs_a_time():
    manifest = _manifest()
    _evaluate_all(manifest)
    _entry(manifest, 32).update(human_decision="approved")
    assert any("decided_at" in p for p in hm.validate_manifest(manifest))


# ---- DB との照合 ----------------------------------------------------------------

def _db(tmp_path, manifest):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    by_hash = {}
    for entry in manifest["entries"]:
        conn.execute("INSERT INTO approval_requests (id, kind, payload_json, status, created_at) "
                     "VALUES (?, 'plugin', ?, 'approved', ?)",
                     (entry["old_approval_id"], json.dumps({
                         "name": entry["name"], "artifact_hash": entry["artifact_hash"],
                         "content_hash": entry["content_hash"]}), at(1).isoformat()))
    for number, entry in enumerate(e for e in manifest["entries"]
                                   if e["new_reevaluation_approval_id"] is not None):
        new_id = entry["new_reevaluation_approval_id"]
        if conn.execute("SELECT 1 FROM approval_requests WHERE id=?", (new_id,)).fetchone():
            continue
        conn.execute(
            "INSERT INTO approval_requests (id, kind, payload_json, status, created_at) "
            "VALUES (?, 'plugin', ?, 'pending', ?)",
            (new_id, json.dumps({
                "name": entry["name"], "artifact_hash": entry["artifact_hash"],
                "content_hash": entry["content_hash"], "candidate_origin": "reevaluation",
                "history_guard": ready_evidence(), "eval_source": "dukascopy",
                "base_interval": "1m", "pairs": ["USDJPY"]}), at(2).isoformat()))
    conn.commit()
    return conn


def test_the_database_agrees_with_an_evaluated_manifest(tmp_path):
    manifest = _manifest()
    _evaluate_all(manifest)
    conn = _db(tmp_path, manifest)
    assert hm.verify_against_db(manifest, conn, no_bars_path=tmp_path / "none.yaml") == []
    conn.close()


def test_a_new_approval_without_valid_evidence_or_of_another_version_is_flagged(tmp_path):
    manifest = _manifest()
    _evaluate_all(manifest)
    conn = _db(tmp_path, manifest)
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=200").fetchone()
    payload = json.loads(row[0])
    payload["history_guard"] = None
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=200", (json.dumps(payload),))
    payload["name"] = "someone_else"
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=201", (json.dumps(payload),))
    conn.commit()
    problems = hm.verify_against_db(manifest, conn, no_bars_path=tmp_path / "none.yaml")
    assert any("no valid history evidence" in p for p in problems)
    assert any("not this version's re-evaluation" in p for p in problems)
    conn.close()


def test_a_recorded_decision_must_match_the_database(tmp_path):
    manifest = _manifest()
    _evaluate_all(manifest)
    for old in (32, 92):
        _entry(manifest, old).update(human_decision="approved", decided_at=NOW.isoformat())
    conn = _db(tmp_path, manifest)
    problems = hm.verify_against_db(manifest, conn, no_bars_path=tmp_path / "none.yaml")
    assert any("recorded decision differs" in p for p in problems)
    conn.close()

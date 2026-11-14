"""従来 (schema_version=1) の manifest で受理・拒否されていた入力の fixture 群。

期待値は、版別対応を入れる前の `validate_manifest` / `verify_against_db` を同じ入力で
実行して得た結果を逐語で固定したもの。v1 の判定は版別分岐の追加後も変えない。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from agentic_fx.backtest import history_migration as hm
from agentic_fx.store.db import connect, init_db
from tests.history_support import ready_evidence
from tests.plugin.deploy_world import at

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
JST = "2026-11-11T21:00:00+09:00"
DEPLOYED_VERSIONS = ("3b35b61fa798", "127825cc55df", "0b5ec2c155db", "ee670b8630f6")


def versions():
    out = {}
    for approval_id, _name, prefix, _deployed in hm.EXPECTED_ENTRIES:
        out[approval_id] = {"artifact_hash": prefix + "a" * (64 - len(prefix)),
                            "content_hash": "c" * 64, "dependency_pins": {}}
    return out


def base_manifest():
    return hm.initial_manifest(base_commit="e63f8e44", backup_id="bk-1", recorded_by="ops",
                               now=NOW, versions=versions())


def entry(manifest, approval_id):
    return next(e for e in manifest["entries"] if e["old_approval_id"] == approval_id)


def evaluate_all(manifest, *, time=None):
    for number, version in enumerate(DEPLOYED_VERSIONS):
        for e in manifest["entries"]:
            if e["artifact_hash"].startswith(version):
                e.update(result_status="evaluated", evaluated_at=time or NOW.isoformat(),
                         new_reevaluation_approval_id=200 + number, evidence_ref="approval:200")


def _unevaluable(m, reason="history_stale"):
    for e in m["entries"]:
        if e["name"] == "volume_close_pressure":
            e.update(result_status="unevaluable", new_reevaluation_approval_id=None,
                     unevaluable_reason=reason, evaluated_at=NOW.isoformat(),
                     evidence_ref="note")


def _nondeployed(m):
    return [e for e in m["entries"] if not e["deployed_at_inventory"]]


def _deployed_evaluated(m):
    evaluate_all(m)


def _jst_everywhere(m):
    evaluate_all(m, time=JST)
    m["created_at"] = m["updated_at"] = JST
    for e in m["entries"]:
        if e["human_decision"] == "pending" and False:
            pass


def _pending_with_stray_fields(m):
    for e in m["entries"]:
        if e["deployed_at_inventory"]:
            e.update(evaluated_at="not a time", evidence_ref=7)


def _deferred_with_stray_fields(m):
    for e in _nondeployed(m):
        e.update(evaluated_at=JST, evidence_ref="x", decided_at="garbage")


def _decided_non_utc(m):
    evaluate_all(m)
    entry(m, 32).update(human_decision="approved", decided_at=JST)


def _naive_evaluated_at(m):
    evaluate_all(m)
    entry(m, 32).update(evaluated_at="2026-11-11T12:00:00")
    entry(m, 92).update(evaluated_at="2026-11-11T12:00:00")


def _unevaluable_other_reason(m):
    _unevaluable(m, reason="artifact_missing")


def _later_reasons(m):
    for reason in ("dependency_changed",):
        _unevaluable(m, reason=reason)


# (id, 変形, 種別)。種別 "validate" は validate_manifest、"verify:<variant>" は DB 照合。
VALIDATE_CASES = [
    ("initial", lambda m: None),
    ("evaluated", _deployed_evaluated),
    ("jst-everywhere", _jst_everywhere),
    ("naive-evaluated-at", _naive_evaluated_at),
    ("unevaluable-fixed-reason", _unevaluable),
    ("unevaluable-new-v2-reason", _unevaluable_other_reason),
    ("unevaluable-dependency-changed", _later_reasons),
    ("pending-with-stray-fields", _pending_with_stray_fields),
    ("deferred-with-stray-fields", _deferred_with_stray_fields),
    ("decided-non-utc", _decided_non_utc),
    ("schema-true", lambda m: m.update(schema_version=True)),
    ("schema-float", lambda m: m.update(schema_version=1.0)),
    ("schema-three", lambda m: m.update(schema_version=3)),
    ("schema-string", lambda m: m.update(schema_version="1")),
    ("schema-float-two", lambda m: m.update(schema_version=2.0)),
    ("created-naive", lambda m: m.update(created_at="2026-11-11T12:00:00")),
    ("created-non-utc", lambda m: m.update(updated_at=JST)),
    ("extra-top", lambda m: m.update(extra=1)),
    ("missing-top", lambda m: m.pop("recorded_by")),
    ("entry-extra-key", lambda m: entry(m, 32).update(audit_ref=None)),
    ("entry-missing-key", lambda m: entry(m, 32).pop("decided_at")),
    ("not-dict", lambda m: m.update(entries=[1] * 15)),
    ("short", lambda m: m["entries"].pop()),
    ("dup", lambda m: m["entries"].append(copy.deepcopy(m["entries"][0]))),
    ("wrong-id", lambda m: m["entries"][0].update(old_approval_id=999)),
    ("short-hash", lambda m: entry(m, 32).update(artifact_hash="3b35b61fa798")),
    ("name", lambda m: entry(m, 32).update(name="other")),
    ("nondeployed-evaluated", lambda m: entry(m, 21).update(
        result_status="evaluated", new_reevaluation_approval_id=3, evaluated_at=NOW.isoformat(),
        evidence_ref="x")),
    ("nondeployed-pending", lambda m: entry(m, 21).update(result_status="pending")),
    ("nondeployed-new-id", lambda m: entry(m, 21).update(new_reevaluation_approval_id=5)),
    ("nondeployed-unevaluable", lambda m: entry(m, 21).update(
        result_status="unevaluable", unevaluable_reason="history_stale")),
    ("decision-without-time", lambda m: (evaluate_all(m), entry(m, 32).update(
        human_decision="approved"))),
    ("unknown-decision", lambda m: entry(m, 32).update(human_decision="maybe")),
    ("unknown-status", lambda m: entry(m, 32).update(result_status="done")),
    ("pair-differs", lambda m: (evaluate_all(m), entry(m, 92).update(
        new_reevaluation_approval_id=999))),
    ("evaluated-no-ref", lambda m: (evaluate_all(m), entry(m, 32).update(evidence_ref=""))),
    ("evaluated-no-time", lambda m: (evaluate_all(m), entry(m, 32).update(evaluated_at=None))),
]
VERIFY_CASES = ["evaluated", "initial", "jst-everywhere", "unevaluable-fixed-reason"]


def build(case_id):
    m = base_manifest()
    dict(VALIDATE_CASES)[case_id](m)
    return m


def make_db(tmp_path, manifest, *, variant="match"):
    conn = connect(tmp_path / f"v-{variant}.db")
    init_db(conn)
    for e in manifest["entries"]:
        payload = {"name": e["name"], "artifact_hash": e["artifact_hash"],
                   "content_hash": e["content_hash"]}
        if variant == "hash" and e["old_approval_id"] == 21:
            payload["content_hash"] = "d" * 64
        if variant == "name" and e["old_approval_id"] == 32:
            payload["name"] = "someone"
        if variant == "missing" and e["old_approval_id"] == 65:
            continue
        conn.execute("INSERT INTO approval_requests (id, kind, payload_json, status, created_at) "
                     "VALUES (?, 'plugin', ?, 'approved', ?)",
                     (e["old_approval_id"], json.dumps(payload), at(1).isoformat()))
    for e in manifest["entries"]:
        new_id = e["new_reevaluation_approval_id"]
        if new_id is None or conn.execute(
                "SELECT 1 FROM approval_requests WHERE id=?", (new_id,)).fetchone():
            continue
        guard = None if variant == "noevidence" else ready_evidence()
        conn.execute("INSERT INTO approval_requests (id, kind, payload_json, status, created_at) "
                     "VALUES (?, 'plugin', ?, ?, ?)",
                     (new_id, json.dumps({
                         "name": e["name"], "artifact_hash": e["artifact_hash"],
                         "content_hash": e["content_hash"], "candidate_origin": "reevaluation",
                         "history_guard": guard, "eval_source": "dukascopy",
                         "base_interval": "1m", "pairs": ["USDJPY"]}),
                      "approved" if variant == "decided" else "pending", at(2).isoformat()))
    conn.commit()
    return conn


DB_VARIANTS = ["match", "hash", "name", "missing", "noevidence", "decided"]

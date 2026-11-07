"""``reevaluate_deployed``: evaluate a deployed strategy again and file the evidence.

The gate itself is replaced by ``FakeGate`` here (it records calls and returns canned rows
and metrics); the real gate runs in ``test_reevaluate_deployed_real.py``. Every database,
plugins directory and lock below lives under ``tmp_path``.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone

import pytest

from agentic_fx.core.timeutil import to_display
from agentic_fx.plugin import loader, switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import backtest_runs as backtest_runs_store
from tests.plugin.deploy_world import World, at
from tests.plugin.reevaluation_world import (
    BASE, SOURCE, STARTED, FakeGate, real_settings)

NAME = "sma"
EPOCH = "time_repair_dst_v1"


class Recorder:
    def __init__(self):
        self.events = []

    def write(self, category, event, summary, **_kwargs):
        self.events.append((event, summary))

    def names(self):
        return [event for event, _summary in self.events]


class Deployed:
    """A deployed, approved strategy (optionally with an indicator it depends on)."""

    def __init__(self, tmp_path, *, indicator: bool = False, epoch: bool = True):
        self.world = World(tmp_path)
        self.conn, self.root = self.world.conn, self.world.root
        self.settings = real_settings()
        self.activity = Recorder()
        if epoch:
            self.set_epoch(EPOCH, 0)
        self.indicator = None
        if indicator:
            self.indicator = self.world.version("ind", "i", kind="indicator")
            self.world.deploy("ind", self.indicator, None, 1)
        self.version = self.world.version(
            NAME, "a", kind="strategy", indicator="ind" if indicator else None,
            pin=loader.content_hash(self.indicator) if indicator else None)
        self.basis_id = self.world.deploy(NAME, self.version, None, 2)
        self.content = loader.content_hash(self.version)
        self.artifact = self.version.name

    def set_epoch(self, name: str, minute: int) -> None:
        self.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                          (name, at(minute).isoformat()))
        self.conn.commit()

    def reevaluate(self, gate: FakeGate, *, name: str = NAME, now=STARTED, settings=None):
        return switch.reevaluate_deployed(
            self.conn, name=name, plugins_root=self.root, settings=settings or self.settings,
            now=now, activity=self.activity, gate_fn=gate)

    def requests(self, status: str | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM approval_requests WHERE "
            "json_extract(payload_json,'$.candidate_origin')='reevaluation' ORDER BY id")
        out = [dict(r) for r in rows]
        return [r for r in out if status is None or r["status"] == status]

    def payload(self, approval_id: int) -> dict:
        return json.loads(self.conn.execute(
            "SELECT payload_json FROM approval_requests WHERE id=?",
            (approval_id,)).fetchone()[0])

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM backtest_runs ORDER BY id")]

    def snapshot(self):
        return (self.world.snapshot(NAME),
                [tuple(r) for r in self.conn.execute("SELECT * FROM backtest_runs")],
                [tuple(r) for r in self.conn.execute("SELECT * FROM approval_requests")])


@pytest.fixture
def dep(tmp_path):
    return Deployed(tmp_path)


# ------------------------------------------------------------------ the request

def test_a_deployed_strategy_gets_a_pending_request_with_its_evidence(dep):
    gate = FakeGate()

    result = dep.reevaluate(gate)

    assert (result.name, result.status) == (NAME, "created")
    (request,) = dep.requests()
    assert request["id"] == result.approval_id and request["status"] == "pending"
    assert request["kind"] == "plugin" and request["expires_at"] is None
    payload = dep.payload(result.approval_id)
    assert payload["candidate_origin"] == "reevaluation"
    assert payload["candidate_path"] == f"plugins/.versions/{NAME}/{dep.artifact}"
    assert (payload["name"], payload["kind"], payload["content_hash"],
            payload["artifact_hash"]) == (NAME, "strategy", dep.content, dep.artifact)
    assert payload["reason"] == "history_time_repaired"
    assert payload["history_epoch"] == EPOCH
    assert payload["reevaluates_approval_id"] == dep.basis_id
    assert payload["basis_decision_id"] == dep.basis_id
    assert payload["deploy_generation"] == switch.deploy_generation(dep.conn, NAME)
    assert payload["in_sample"] == {"USDJPY": gate.metrics("in_sample")}
    assert payload["holdout"] == {"USDJPY": gate.metrics("holdout")}
    assert (payload["eval_source"], payload["base_interval"], payload["eval_timeframe"],
            payload["live_source"]) == (SOURCE, BASE, "1h", dep.settings.datafeed.primary)
    assert payload["indicator_deps"] == {}
    assert payload["floor_warning"] == "" and "profitability_floor" in payload
    # Nothing is inherited from the improvement ledger.
    for key in ("mission_id", "backlog_id", "candidate_archive_id"):
        assert key not in payload
    # The deployment is untouched.
    assert os.readlink(dep.root / NAME) == f".versions/{NAME}/{dep.artifact}"


def test_the_run_ids_point_at_the_rows_whose_metrics_are_in_the_payload(dep):
    gate = FakeGate()
    result = dep.reevaluate(gate)
    payload = dep.payload(result.approval_id)

    runs = {r["id"]: r for r in dep.runs()}
    assert sorted(runs) == [1, 2]
    for scope, key in (("in_sample", "in_sample"), ("holdout_gate", "holdout")):
        run_id = payload["run_ids"][key]["USDJPY"]
        row = runs[run_id]
        assert (row["scope"], row["mission_outcome"], row["variant"]) == (
            scope, "reevaluation", "candidate")
        assert json.loads(row["metrics_json"]) == payload[key]["USDJPY"]
        assert row["content_hash"] == dep.content


def test_a_floor_warning_is_carried_into_the_request(dep):
    result = dep.reevaluate(FakeGate(floor="unprofitable"))

    payload = dep.payload(result.approval_id)
    assert (result.status, payload["floor_warning"], payload["floor_detail"]) == (
        "created", "unprofitable", "detail")


def test_the_evaluation_time_is_the_shared_now_cut_to_the_base_grid(dep):
    dep.settings.backtest.base_interval = "1m"
    started = datetime(2026, 11, 8, 3, 17, 42, tzinfo=timezone.utc)

    result = dep.reevaluate(FakeGate(), now=started)

    eval_now = dep.payload(result.approval_id)["eval_now"]
    assert datetime.fromisoformat(eval_now) == datetime(2026, 11, 8, 3, 17, tzinfo=timezone.utc)
    shown = to_display(datetime.fromisoformat(eval_now), "Asia/Tokyo")
    assert shown.isoformat() == "2026-11-08T12:17:00+09:00"


def test_the_gate_is_given_the_shared_now_and_a_fixed_input(dep):
    gate = FakeGate()

    dep.reevaluate(gate)

    assert gate.calls == [{"name": NAME, "now": STARTED}]


# ------------------------------------------------------------------ uniqueness

def test_asking_again_files_nothing_and_does_not_evaluate(dep):
    first = dep.reevaluate(FakeGate())
    gate = FakeGate()

    second = dep.reevaluate(gate)

    assert (second.status, second.approval_id) == ("existing", first.approval_id)
    assert gate.calls == []
    assert len(dep.requests("pending")) == 1
    assert len(dep.runs()) == 2


def test_a_request_with_another_key_is_superseded_by_the_new_one(dep):
    first = dep.reevaluate(FakeGate())
    dep.set_epoch("time_repair_dst_v2", 5)

    second = dep.reevaluate(FakeGate())

    assert (second.status, second.approval_id) != ("existing", first.approval_id)
    assert second.status == "created"
    old = dep.requests()[0]
    assert (old["id"], old["status"], old["reason"], old["decided_by"]) == (
        first.approval_id, "invalidated", "reevaluation_superseded", "system")
    assert [r["id"] for r in dep.requests("pending")] == [second.approval_id]


@pytest.mark.parametrize("change", ["generation", "decision"])
def test_every_part_of_the_key_makes_a_new_request(dep, change):
    first = dep.reevaluate(FakeGate())
    if change == "generation":
        dep.world.journal(NAME, "approve", dep.version, dep.version, 7)
    else:
        later = approvals_store.create(
            dep.conn, "plugin", {"name": NAME, "content_hash": dep.content,
                                 "artifact_hash": dep.artifact}, at(7))
        approvals_store.apply_decision(dep.conn, later, "approved", decided_by="human",
                                       now=at(7))

    second = dep.reevaluate(FakeGate())

    assert second.status == "created" and second.approval_id != first.approval_id
    assert len(dep.requests("pending")) == 1


def test_a_request_for_an_earlier_version_is_superseded_when_another_is_deployed(dep):
    first = dep.reevaluate(FakeGate())
    newer = dep.world.version(NAME, "b", kind="strategy")
    dep.world.deploy(NAME, newer, dep.version, 8)

    second = dep.reevaluate(FakeGate())

    assert second.status == "created" and second.approval_id != first.approval_id
    assert dep.payload(second.approval_id)["artifact_hash"] == newer.name
    assert [r["id"] for r in dep.requests("pending")] == [second.approval_id]
    assert dep.requests()[0]["reason"] == "reevaluation_superseded"


def test_the_stored_request_is_checked_again_inside_the_save_transaction(dep, monkeypatch):
    """Another process can file the same request while this one evaluates."""
    other = {}

    def file_the_same_request():
        probe = FakeGate()
        original = switch.reevaluate_deployed
        # a second process takes the snapshot, evaluates and saves first
        other["result"] = original(
            dep.conn, name=NAME, plugins_root=dep.root, settings=dep.settings,
            now=STARTED, activity=dep.activity, gate_fn=probe)

    result = dep.reevaluate(FakeGate(during=file_the_same_request))

    assert other["result"].status == "created"
    assert (result.status, result.approval_id) == ("existing", other["result"].approval_id)
    assert len(dep.requests()) == 1
    assert len(dep.runs()) == 2


def test_another_name_keeps_its_own_pending_request(tmp_path):
    dep = Deployed(tmp_path)
    other = dep.world.version("zeta", "z", kind="strategy")
    dep.world.deploy("zeta", other, None, 3)
    a = dep.reevaluate(FakeGate(), name=NAME)

    b = dep.reevaluate(FakeGate(), name="zeta")

    assert (a.status, b.status) == ("created", "created")
    assert len(dep.requests("pending")) == 2


# ------------------------------------------------------------------ changed while evaluating

def _edit_the_version(dep):
    plugin = dep.version / "plugin.py"
    plugin.write_text(plugin.read_text() + "# edited in place\n")


def _relink_to_another_version(dep):
    dep.world.link(NAME, dep.world.version(NAME, "b", kind="strategy"))


def _approve_again(dep):
    later = approvals_store.create(
        dep.conn, "plugin", {"name": NAME, "content_hash": dep.content,
                             "artifact_hash": dep.artifact}, at(8))
    approvals_store.apply_decision(dep.conn, later, "approved", decided_by="human", now=at(8))


def _revoke(dep):
    approvals_store.append_revocation(dep.conn, NAME, dep.content, now=at(8))


def _change_the_epoch(dep):
    dep.set_epoch("time_repair_dst_v2", 9)


def _round_trip(dep):
    other = dep.world.version(NAME, "b", kind="strategy")
    dep.world.link(NAME, other)
    dep.world.journal(NAME, "approve", dep.version, other, 5)
    dep.world.link(NAME, dep.version)
    dep.world.journal(NAME, "approve", other, dep.version, 6)


CHANGES = {
    "artifact_edited": _edit_the_version,
    "switched": _relink_to_another_version,
    "approved_again": _approve_again,
    "revoked": _revoke,
    "epoch": _change_the_epoch,
    "round_trip": _round_trip,
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_change_during_the_evaluation_discards_the_result(dep, change):
    before = dep.snapshot()
    gate = FakeGate(during=lambda: CHANGES[change](dep))

    result = dep.reevaluate(gate)

    assert result.status == "changed_during_evaluation"
    assert dep.requests() == []
    assert dep.runs() == []
    assert "reevaluation_changed_during_evaluation" in dep.activity.names()
    assert dep.snapshot()[1] == before[1]


def test_a_dependency_pin_change_during_the_evaluation_discards_the_result(tmp_path):
    dep = Deployed(tmp_path, indicator=True)

    def new_indicator_version():
        newer = dep.world.version("ind", "j", kind="indicator")
        dep.world.deploy("ind", newer, dep.indicator, 6)

    result = dep.reevaluate(FakeGate(during=new_indicator_version))

    assert result.status == "changed_during_evaluation"
    assert dep.requests() == [] and dep.runs() == []


def test_a_dependent_strategy_files_its_pins_in_the_request(tmp_path):
    dep = Deployed(tmp_path, indicator=True)

    result = dep.reevaluate(FakeGate())

    assert result.status == "created"
    pins = dep.payload(result.approval_id)["indicator_deps"]
    assert pins == {"fast": {"plugin": "ind", "content_hash": loader.content_hash(dep.indicator),
                             "params": {}}}


def test_the_save_transaction_checks_the_epoch_once_more(dep, monkeypatch):
    """A repair that commits after the locked re-check but before the transaction is caught."""
    real = switch._store_reevaluation

    def repair_then_store(conn, snapshot, *args, **kwargs):
        dep.set_epoch("time_repair_dst_v2", 9)
        return real(conn, snapshot, *args, **kwargs)

    monkeypatch.setattr(switch, "_store_reevaluation", repair_then_store)

    result = dep.reevaluate(FakeGate())

    assert result.status == "changed_during_evaluation"
    assert dep.requests() == [] and dep.runs() == []


def test_the_unevaluable_rows_are_saved_only_if_the_epoch_still_holds(dep, monkeypatch):
    real = switch._store_unevaluable_rows

    def repair_then_store(conn, snapshot, rows):
        dep.set_epoch("time_repair_dst_v2", 9)
        return real(conn, snapshot, rows)

    monkeypatch.setattr(switch, "_store_unevaluable_rows", repair_then_store)

    result = dep.reevaluate(FakeGate(evaluable=False, reason="insufficient_trades:3"))

    assert result.status == "changed_during_evaluation"
    assert dep.runs() == []
    assert "reevaluation_unevaluable" not in dep.activity.names()


# ------------------------------------------------------------------ evaluation outside the locks

def test_the_evaluation_runs_outside_every_lock(dep):
    held = {}

    def probe():
        for label, path in (("name", switch.plugin_lock_path(dep.root, NAME)),
                            ("guard", switch.deploy_guard_path(dep.root))):
            handle = open(path, "a+")
            try:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held[label] = False
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except BlockingIOError:
                    held[label] = True
            finally:
                handle.close()

    result = dep.reevaluate(FakeGate(during=probe))

    assert result.status == "created"
    assert held == {"name": False, "guard": False}


def test_a_producer_style_lock_wait_succeeds_during_the_evaluation(dep):
    import time
    waited = {}

    def probe():
        start = time.monotonic()
        with switch._plugin_locks(dep.root, [NAME], deadline=time.monotonic() + 0.5):
            waited["seconds"] = time.monotonic() - start

    dep.reevaluate(FakeGate(during=probe))

    assert waited["seconds"] < 0.4


def test_the_snapshot_is_taken_and_the_result_saved_under_the_locks(dep, monkeypatch):
    seen = []
    real_snapshot = switch._take_reevaluation_snapshot
    real_recheck = switch._snapshot_is_current
    real_store = switch._store_reevaluation

    def lock_state() -> bool:
        handle = open(switch.plugin_lock_path(dep.root, NAME), "a+")
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        finally:
            handle.close()

    monkeypatch.setattr(switch, "_take_reevaluation_snapshot",
                        lambda *a, **k: (seen.append(("snapshot", lock_state())),
                                         real_snapshot(*a, **k))[1])
    monkeypatch.setattr(switch, "_snapshot_is_current",
                        lambda *a, **k: (seen.append(("recheck", lock_state())),
                                         real_recheck(*a, **k))[1])
    monkeypatch.setattr(switch, "_store_reevaluation",
                        lambda *a, **k: (seen.append(("store", lock_state())),
                                         real_store(*a, **k))[1])

    dep.reevaluate(FakeGate())

    assert seen == [("snapshot", True), ("recheck", True), ("store", True)]


# ------------------------------------------------------------------ unevaluable and failed

def test_a_strategy_with_too_few_trades_files_no_request_but_keeps_its_rows(dep):
    before = dep.snapshot()
    gate = FakeGate(evaluable=False, reason="insufficient_trades:3")

    result = dep.reevaluate(gate)

    assert (result.status, result.reason, result.approval_id) == (
        "unevaluable", "insufficient_trades:3", None)
    assert dep.requests() == []
    runs = dep.runs()
    assert [(r["scope"], r["mission_outcome"]) for r in runs] == [("in_sample", "reevaluation")]
    assert dep.activity.events == [
        ("reevaluation_unevaluable", f"name={NAME} reason=insufficient_trades:3")]
    assert dep.snapshot()[0] == before[0] and dep.snapshot()[2] == before[2]


def test_an_unevaluable_result_without_rows_leaves_no_rows(dep):
    result = dep.reevaluate(FakeGate(evaluable=False, reason="history_source_unconfigured",
                                     rows=False))

    assert result.status == "unevaluable"
    assert dep.runs() == [] and dep.requests() == []
    assert dep.activity.names() == ["reevaluation_unevaluable"]


def test_an_unevaluable_result_is_discarded_when_the_epoch_moved_meanwhile(dep):
    result = dep.reevaluate(FakeGate(evaluable=False, reason="insufficient_trades:3",
                                     during=lambda: _change_the_epoch(dep)))

    assert result.status == "changed_during_evaluation"
    assert dep.runs() == []


def test_a_failing_evaluation_files_nothing(dep):
    result = dep.reevaluate(FakeGate(error=RuntimeError("worker died")))

    assert (result.status, result.approval_id) == ("failed", None)
    assert dep.requests() == [] and dep.runs() == []
    (event, summary), = dep.activity.events
    assert event == "reevaluation_failed" and f"name={NAME}" in summary
    assert os.readlink(dep.root / NAME) == f".versions/{NAME}/{dep.artifact}"


def test_a_lock_that_stays_held_is_a_failure_not_a_hang(dep):
    switch.plugin_lock_path(dep.root, NAME).parent.mkdir(parents=True, exist_ok=True)
    holder = open(switch.plugin_lock_path(dep.root, NAME), "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = switch.reevaluate_deployed(
            dep.conn, name=NAME, plugins_root=dep.root, settings=dep.settings, now=STARTED,
            activity=dep.activity, gate_fn=FakeGate(), lock_wait_sec=0.2)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert result.status == "failed"
    assert dep.requests() == [] and dep.runs() == []


def test_a_failure_while_saving_leaves_neither_rows_nor_request(dep, monkeypatch):
    def failing(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(approvals_store, "create", failing)

    result = dep.reevaluate(FakeGate())

    assert result.status == "failed"
    assert dep.runs() == []
    assert dep.requests() == []
    assert dep.conn.in_transaction is False
    assert dep.activity.names() == ["reevaluation_failed"]


def test_a_failure_while_saving_keeps_the_earlier_pending_request(dep, monkeypatch):
    first = dep.reevaluate(FakeGate())
    dep.set_epoch("time_repair_dst_v2", 5)

    def failing(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(approvals_store, "create", failing)

    result = dep.reevaluate(FakeGate())

    assert result.status == "failed"
    assert [r["id"] for r in dep.requests("pending")] == [first.approval_id]
    assert len(dep.runs()) == 2


def test_the_gate_rows_are_written_by_the_shared_writer(dep, monkeypatch):
    calls = []
    real = switch._write_gate_rows
    monkeypatch.setattr(switch, "_write_gate_rows",
                        lambda conn, rows, **kw: (calls.append((len(rows), kw)),
                                                  real(conn, rows, **kw))[1])

    result = dep.reevaluate(FakeGate())

    assert result.status == "created"
    assert calls == [(2, {"mission_outcome": "reevaluation"})]
    payload = json.loads(dep.requests("pending")[0]["payload_json"])
    assert payload["run_ids"] == {"in_sample": {"USDJPY": dep.runs()[0]["id"]},
                                  "holdout": {"USDJPY": dep.runs()[1]["id"]}}


def test_the_rows_and_the_request_are_one_transaction(dep, monkeypatch):
    seen = []
    real_create = approvals_store.create

    def watching(conn, *args, **kwargs):
        seen.append(("create", kwargs.get("commit"), conn.in_transaction,
                     conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]))
        return real_create(conn, *args, **kwargs)

    monkeypatch.setattr(approvals_store, "create", watching)

    dep.reevaluate(FakeGate())

    # the rows were written before the request, inside the same open transaction
    assert seen == [("create", False, True, 2)]


# ------------------------------------------------------------------ what is skipped

def test_an_indicator_is_skipped(tmp_path):
    dep = Deployed(tmp_path, indicator=True)

    result = dep.reevaluate(FakeGate(), name="ind")

    assert (result.status, result.reason) == ("skipped", "not_strategy")


def test_an_indicator_is_skipped_without_building_the_inventory(tmp_path, monkeypatch):
    dep = Deployed(tmp_path, indicator=True)
    builds = []
    real = switch.tools_plugin_loader.approved_plugins
    monkeypatch.setattr(switch.tools_plugin_loader, "approved_plugins",
                        lambda *a, **k: (builds.append(1), real(*a, **k))[1])

    dep.reevaluate(FakeGate(), name="ind")

    assert builds == []


def test_one_evaluation_builds_the_inventory_once(dep, monkeypatch):
    builds = []
    real = switch.tools_plugin_loader.approved_plugins
    monkeypatch.setattr(switch.tools_plugin_loader, "approved_plugins",
                        lambda *a, **k: (builds.append(1), real(*a, **k))[1])

    result = dep.reevaluate(FakeGate())

    assert result.status == "created"
    assert builds == [1]


def test_a_name_that_is_not_deployed_is_skipped(dep):
    assert dep.reevaluate(FakeGate(), name="nothing") == switch.ReevaluationResult(
        "nothing", "skipped", reason="not_deployed")


def test_a_plain_directory_is_skipped(dep):
    plain = dep.root / "plainone"
    plain.mkdir()
    for file in ("plugin.py", "config.yaml", "test_plugin.py"):
        (plain / file).write_text((dep.version / file).read_text())

    assert dep.reevaluate(FakeGate(), name="plainone").reason == "plain_plugin"


def test_a_version_whose_latest_decision_is_not_an_approval_is_skipped(dep):
    approvals_store.append_revocation(dep.conn, NAME, dep.content, now=at(8))

    result = dep.reevaluate(FakeGate())

    assert (result.status, result.reason) == ("skipped", "not_approved")


def test_the_scan_lists_canonical_entries_in_name_order(dep):
    (dep.root / "_human").mkdir()
    (dep.root / "Bad-Name").mkdir()
    other = dep.world.version("alpha", "x", kind="strategy")
    dep.world.deploy("alpha", other, None, 3)

    assert switch.reevaluation_candidate_names(dep.root) == ["alpha", NAME]


# ------------------------------------------------------------------ the previous evidence

def _set_basis_payload(dep: Deployed, **fields) -> None:
    payload = dep.payload(dep.basis_id)
    payload.update(fields)
    dep.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                     (json.dumps(payload), dep.basis_id))
    dep.conn.commit()


def _approval_run(dep: Deployed, *, created_at=None, scope="holdout_gate", pair="USDJPY",
                  content=None, variant="candidate", outcome="approval", source=SOURCE,
                  metrics=None) -> int:
    run_id = backtest_runs_store.save_harness_run(
        dep.conn, scope=scope, plugin_ref=f"plugins/{NAME}",
        content_hash=content or dep.content, kind="strategy", pair=pair, timeframe="1h",
        source=source, base_interval=BASE, period=(at(0), at(1)),
        metrics=metrics or {"trades": 33, "pf": 1.1}, settings_hash="s", core_commit="c",
        initial_balance=1e6, now=at(1), variant=variant, mission_outcome=outcome)
    if created_at is not None:
        dep.conn.execute("UPDATE backtest_runs SET created_at=? WHERE id=?",
                         (created_at, run_id))
        dep.conn.commit()
    return run_id


def _previous(dep: Deployed) -> dict:
    result = dep.reevaluate(FakeGate())
    assert result.status == "created"
    return dep.payload(result.approval_id)["previous"]


def test_the_previous_in_sample_of_a_human_approval_comes_from_its_payload(dep):
    metrics = {"USDJPY": {"trades": 50, "pf": 1.4}}
    _set_basis_payload(dep, metrics=metrics)

    previous = _previous(dep)

    assert previous["approval_id"] == dep.basis_id
    assert previous["in_sample"] == {"USDJPY": {
        "metrics": metrics["USDJPY"], "source": "payload", "run_id": None}}


def test_the_previous_in_sample_of_an_improve_approval_comes_from_its_payload(dep):
    _set_basis_payload(
        dep, in_sample={"USDJPY": {"trades": 51, "pf": 1.5}},
        holdout={"trades": 44, "pf": 1.2})

    previous = _previous(dep)

    assert previous["in_sample"]["USDJPY"]["metrics"] == {"trades": 51, "pf": 1.5}
    assert previous["holdout"]["USDJPY"] == {
        "metrics": {"trades": 44, "pf": 1.2}, "source": "payload", "run_id": None}


def test_values_the_payload_does_not_hold_are_unknown(dep):
    previous = _previous(dep)

    unknown = {"metrics": None, "source": "unknown", "run_id": None}
    assert previous["in_sample"] == {"USDJPY": unknown}
    assert previous["holdout"] == {"USDJPY": unknown}


def test_a_previous_holdout_is_recovered_from_the_one_matching_run(dep):
    run_id = _approval_run(dep, metrics={"trades": 33, "pf": 1.1})

    previous = _previous(dep)

    assert previous["holdout"]["USDJPY"] == {
        "metrics": {"trades": 33, "pf": 1.1}, "source": "run", "run_id": run_id}


def test_two_matching_runs_make_the_previous_holdout_unknown(dep):
    _approval_run(dep)
    _approval_run(dep, metrics={"trades": 99, "pf": 9.0})

    assert _previous(dep)["holdout"]["USDJPY"]["source"] == "unknown"


@pytest.mark.parametrize("stray", [
    {"content": "f" * 64}, {"pair": "EURUSD"}, {"variant": "baseline"},
    {"outcome": None}, {"outcome": "reevaluation"}, {"scope": "in_sample"},
    {"source": "mt5"},
], ids=lambda s: next(iter(s)) + "=" + str(next(iter(s.values()))))
def test_a_run_that_is_not_the_approval_gate_row_is_not_the_previous_holdout(dep, stray):
    _approval_run(dep, **stray)

    assert _previous(dep)["holdout"]["USDJPY"]["source"] == "unknown"


def test_a_run_written_after_the_approval_is_not_the_previous_holdout(dep):
    _approval_run(dep, created_at=at(3).isoformat())

    assert _previous(dep)["holdout"]["USDJPY"]["source"] == "unknown"


def test_a_run_written_in_the_same_instant_as_the_approval_counts(dep):
    run_id = _approval_run(dep, created_at=at(2).isoformat())

    assert _previous(dep)["holdout"]["USDJPY"]["run_id"] == run_id


def test_the_times_are_compared_as_times_not_as_text(dep):
    # 12:01 at +09:00 is 03:01Z, before the approval at 03:02Z although the text sorts after it.
    run_id = _approval_run(dep, created_at="2026-10-16T12:01:00+09:00")

    assert _previous(dep)["holdout"]["USDJPY"]["run_id"] == run_id
    other = _approval_run(dep, created_at="2026-10-16T12:03:00+09:00")
    assert other != run_id
    assert _previous_again(dep)["holdout"]["USDJPY"]["run_id"] == run_id


def _previous_again(dep: Deployed) -> dict:
    dep.set_epoch("time_repair_dst_v2", 9)
    return _previous(dep)


def test_the_previous_of_a_re_evaluation_is_the_earlier_request(dep):
    first = dep.reevaluate(FakeGate())
    outcome = switch.approve_candidate(
        dep.conn, first.approval_id, decided_by="human", now=at(20), plugins_root=dep.root,
        settings=dep.settings)
    assert outcome.outcome == "reevaluation_confirmed"

    second = dep.reevaluate(FakeGate(trades=60))

    assert second.status == "created"
    payload = dep.payload(second.approval_id)
    assert payload["reevaluates_approval_id"] == first.approval_id
    assert payload["basis_decision_id"] == first.approval_id
    assert payload["previous"]["approval_id"] == first.approval_id
    assert payload["previous"]["in_sample"]["USDJPY"]["source"] == "payload"
    assert payload["previous"]["in_sample"]["USDJPY"]["metrics"]["trades"] == 40
    assert payload["in_sample"]["USDJPY"]["trades"] == 60

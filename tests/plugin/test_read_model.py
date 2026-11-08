"""plugin read model: 名前の列挙元、3 つの軸、固定語、依存、有用性の数え方。"""
from __future__ import annotations

import json
import os

import pytest

from agentic_fx.plugin import read_model, read_model_inputs
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.plugin.read_model import (
    ADMISSION_EXCLUDED, ADMISSION_LOADED, ADMISSION_NONE, ADMISSION_UNAPPROVED,
    ADMISSION_UNKNOWN, ENTITY_ABSENT, ENTITY_PLAIN, ENTITY_SYMLINK, ENTITY_UNREADABLE,
    build_read_model, detail_to_dto, model_to_dto,
)
from agentic_fx.tools import plugin_loader
from tests.backtest.factories import SETTINGS
from tests.plugin.read_model_world import World, by_name


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def model_of(world, settings=SETTINGS):
    inputs = read_model_inputs.collect_inputs(world.plugins, world.conn)
    return build_read_model(inputs, settings=settings)


def dep(plugin, pin, alias="a"):
    return f"indicators:\n  {alias}: {{plugin: {plugin}, pin: '{pin}'}}\n"


# ---------------------------------------------------------------- 名前の列挙元

def test_a_live_entry_alone_is_listed(world):
    world.deploy("rsi")
    assert [row.name for row in model_of(world).rows] == ["rsi"]


def test_a_version_directory_alone_is_listed(world):
    world.version_only("rsi")
    row = by_name(model_of(world), "rsi")
    assert row.entity.state == ENTITY_ABSENT and len(row.versions) == 1


@pytest.mark.parametrize("status", ["pending", "approved", "rejected", "expired", "invalidated"])
def test_an_approval_request_of_any_status_alone_is_listed(world, status):
    world.raw_request({"name": "ghost", "kind": "indicator", "content_hash": "a" * 64},
                      status=status)
    row = by_name(model_of(world), "ghost")
    assert row.entity.state == ENTITY_ABSENT and row.kind == "indicator"


def test_a_finished_deploy_op_alone_is_listed(world):
    world.deploy_op("gone", phase="done")
    row = by_name(model_of(world), "gone")
    assert row.entity.state == ENTITY_ABSENT and row.open_ops == ()


def test_a_decided_switch_journal_alone_is_listed(world):
    world.journal("gone", 1, phase="decided")
    row = by_name(model_of(world), "gone")
    assert row.open_ops == ()


def test_the_name_left_after_a_completed_disable_is_still_listed(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, phase="decided")
    world.deploy_op("rsi", op_kind="disable_symlink", phase="done")
    world.undeploy("rsi")
    row = by_name(model_of(world), "rsi")
    assert (row.entity.state, row.admission.state, row.open_ops) == (
        ENTITY_ABSENT, ADMISSION_NONE, ())
    assert row.kind == "indicator"          # 承認の payload から


def test_names_that_are_not_in_any_source_are_not_invented(world):
    world.deploy("rsi")
    assert {row.name for row in model_of(world).rows} == {"rsi"}


# ---------------------------------------------------------------- kind

def test_kind_has_four_values_and_signal_is_not_folded_into_unknown(world):
    world.deploy("rsi", "indicator")
    world.deploy("mom", "strategy")
    world.deploy("brk", "signal")
    (world.plugins / "junk").mkdir()
    model = model_of(world)
    assert {row.name: row.kind for row in model.rows} == {
        "rsi": "indicator", "mom": "strategy", "brk": "signal", "junk": "unknown"}
    assert set(read_model.KINDS) == {"strategy", "indicator", "signal", "unknown"}


def _ghost(world, kind, status="pending", name="ghost"):
    return world.raw_request({"name": name, "kind": kind, "content_hash": "a" * 64},
                             status=status)


def test_without_a_readable_live_the_kind_is_the_newest_request_kind(world):
    _ghost(world, "strategy", "rejected")
    _ghost(world, "indicator", "pending")
    assert by_name(model_of(world), "ghost").kind == "indicator"
    _ghost(world, "signal", "approved")
    assert by_name(model_of(world), "ghost").kind == "signal"


def test_the_newest_request_decides_even_when_it_is_not_pending(world):
    _ghost(world, "indicator", "pending")
    _ghost(world, "strategy", "expired")
    assert by_name(model_of(world), "ghost").kind == "strategy"


def test_the_kind_is_unknown_when_the_newest_request_has_no_readable_kind(world):
    _ghost(world, "indicator", "approved")
    world.raw_request({"name": "ghost", "content_hash": "a" * 64})
    assert by_name(model_of(world), "ghost").kind == "unknown"
    world.raw_request({"name": "ghost", "kind": "indicatorx", "content_hash": "a" * 64})
    assert by_name(model_of(world), "ghost").kind == "unknown"


def test_a_name_with_no_request_and_no_live_is_unknown(world):
    world.version_only("rsi")
    assert by_name(model_of(world), "rsi").kind == "unknown"


def test_an_unreadable_live_takes_the_newest_request_kind(world):
    (world.plugins / "rsi").mkdir()
    (world.plugins / "rsi" / "config.yaml").write_text("kind: [oops")
    _ghost(world, "strategy", "rejected", "rsi")
    _ghost(world, "indicator", "pending", "rsi")
    row = by_name(model_of(world), "rsi")
    assert row.entity.state == ENTITY_UNREADABLE and row.kind == "indicator"


def test_a_readable_live_keeps_the_loader_kind_over_any_request(world):
    world.deploy("rsi", "indicator")
    _ghost(world, "strategy", "pending", "rsi")
    row = by_name(model_of(world), "rsi")
    assert row.kind == "indicator" and "kind_mismatch" in row.issues


# ---------------------------------------------------------------- 配備実体

def test_entity_symlink_plain_absent_and_unreadable(world):
    linked = world.deploy("linked")
    world.deploy("plainone", plain=True)
    world.version_only("absentone")
    (world.plugins / "broken").mkdir()
    model = model_of(world)
    assert by_name(model, "linked").entity.state == ENTITY_SYMLINK
    assert by_name(model, "linked").entity.artifact_hash == linked.artifact_hash
    assert by_name(model, "linked").entity.content_hash == linked.content_hash
    assert by_name(model, "plainone").entity.state == ENTITY_PLAIN
    assert by_name(model, "absentone").entity.state == ENTITY_ABSENT
    assert by_name(model, "broken").entity.state == ENTITY_UNREADABLE


@pytest.mark.parametrize("case,reason", [
    ("missing_files", "missing_files"),
    ("invalid_manifest", "invalid_manifest"),
    ("invalid_symlink", "invalid_symlink"),
    ("version_mismatch", "version_mismatch"),
    ("non_canonical_name", "non_canonical_name"),
])
def test_unreadable_entities_carry_a_fixed_reason(world, case, reason):
    name = "Bad-Name" if case == "non_canonical_name" else "victim"
    directory = world.plugins / name
    if case == "missing_files":
        directory.mkdir()
        (directory / "config.yaml").write_text("kind: indicator\n")
    elif case == "invalid_manifest":
        directory.mkdir()
        for item, text in (("plugin.py", "x"), ("config.yaml", "kind: [oops"),
                           ("test_plugin.py", "x")):
            (directory / item).write_text(text)
    elif case == "invalid_symlink":
        os.symlink("../../etc", directory)
    elif case == "version_mismatch":
        meta = world.deploy("victim")
        os.chmod(meta.path, 0o700)
        for item in ("plugin.py", "test_plugin.py"):
            os.chmod(meta.path / item, 0o600)
        (meta.path / "test_plugin.py").write_text("def test_changed():\n    pass\n")
    else:
        directory.mkdir()
    row = by_name(model_of(world), name)
    assert row.entity.state == ENTITY_UNREADABLE and row.entity.reason == reason
    assert row.admission.state == ADMISSION_NONE
    assert reason in read_model.UNREADABLE_REASONS


def test_a_loader_failure_text_never_reaches_the_model_or_the_dto(world):
    world.deploy("rsi")
    secret = "/home/someone/secret-path boom"

    def boom(entry, name):
        raise ValueError(secret)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read_model_inputs.loader, "discover_one_with_reason", boom)
        model = model_of(world)
    text = json.dumps(model_to_dto(model))
    assert secret not in text and "secret" not in text
    assert by_name(model, "rsi").entity.reason == "invalid_manifest"


# ---------------------------------------------------------------- 承認と読込

def test_admission_unapproved_never_approved_and_content_changed(world):
    world.deploy("fresh")
    changed = world.deploy("edited", extra="# a\n")
    old = world.deploy("edited2")
    world.request(old, status="approved")
    world.undeploy("edited2")
    world.deploy("edited2", extra="# b\n")
    world.request(changed, status="rejected")
    model = model_of(world)
    assert by_name(model, "fresh").admission == read_model.Admission(
        ADMISSION_UNAPPROVED, "never_approved")
    assert by_name(model, "edited2").admission == read_model.Admission(
        ADMISSION_UNAPPROVED, "content_changed")
    assert by_name(model, "edited").admission.state == ADMISSION_UNAPPROVED


def test_admission_uses_the_loader_rule_for_the_latest_decision(world):
    meta = world.deploy("rsi")
    world.request(meta, status="approved")
    world.request(meta, status="rejected")           # 後の決定が勝つ
    assert by_name(model_of(world), "rsi").admission.state == ADMISSION_UNAPPROVED
    world.request(meta, status="expired")             # 決定として数えない
    world.request(meta, status="approved")
    assert by_name(model_of(world), "rsi").admission.state == ADMISSION_LOADED


def test_admission_agrees_with_approved_plugins_on_who_is_excluded(world):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    good = world.deploy("good", "strategy", dep("rsi", rsi.content_hash))
    bad = world.deploy("bad", "strategy", dep("rsi", "a" * 64))
    loose = world.deploy("loose", "strategy", "indicators:\n  a: {plugin: rsi}\n")
    missing = world.deploy("missing", "strategy", dep("nothing", "b" * 64))
    for meta in (good, bad, loose):
        world.request(meta, status="approved", inventory=(rsi,))
    world.request(missing, status="approved",
                  deps={"a": {"plugin": "nothing", "content_hash": "b" * 64}})
    model = model_of(world)
    loaded = plugin_loader.approved_plugins(world.conn, world.plugins, settings=SETTINGS)
    admitted = {meta.name for meta in loaded.inventory.metas}
    rejected = {item.name: (item.alias, item.reason) for item in loaded.rejected_strategies}
    for name in ("rsi", "good", "bad", "loose", "missing"):
        row = by_name(model, name)
        assert (row.admission.state == ADMISSION_LOADED) is (name in admitted), name
        if name in rejected:
            assert row.admission.state == ADMISSION_EXCLUDED
            assert (row.admission.alias, row.admission.detail) == rejected[name]
            assert row.admission.reason == "indicator_unresolved"


def test_without_settings_dependent_strategies_are_unknown_not_loaded(world):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    strategy = world.deploy("mom", "strategy", dep("rsi", rsi.content_hash))
    world.request(strategy, status="approved", inventory=(rsi,))
    row = by_name(model_of(world, settings=None), "mom")
    assert row.admission.state == ADMISSION_UNKNOWN
    assert [item.state for item in row.deps] == ["unknown"]
    assert by_name(model_of(world), "mom").admission.state == ADMISSION_LOADED


# ---------------------------------------------------------------- 依存

def test_dependencies_are_listed_for_strategies_and_dependents_for_indicators(world):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    strategy = world.deploy("mom", "strategy", dep("rsi", "a" * 64, alias="fast"))
    world.request(strategy, status="approved", inventory=(rsi,))
    model = model_of(world)
    mom, indicator = by_name(model, "mom"), by_name(model, "rsi")
    assert [(item.alias, item.plugin, item.state) for item in mom.deps] == [
        ("fast", "rsi", "pin_mismatch")]
    assert [(item.strategy, item.alias, item.state) for item in indicator.dependents] == [
        ("mom", "fast", "pin_mismatch")]


def test_an_unapproved_indicator_is_not_in_the_phase_one_inventory(world):
    rsi = world.deploy("rsi")                       # 承認していない
    strategy = world.deploy("mom", "strategy", dep("rsi", rsi.content_hash))
    world.request(strategy, status="approved", inventory=(rsi,))
    assert [item.state for item in by_name(model_of(world), "mom").deps] == ["not_found"]


def test_open_ops_are_shown_without_taking_any_lock(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, phase="recorded", kind="bless")
    world.deploy_op("rsi", op_kind="rollback_symlink", phase="needs_retry",
                    fail_reason="verify_failed", target="e" * 64)      # open ではない
    world.deploy_op("rsi", op_kind="rollback_symlink", phase="intent", target="d" * 64)

    def refuse(*args, **kwargs):
        raise AssertionError("read model must not take the plugin lock")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plugin_switch, "_plugin_lock", refuse)
        row = by_name(model_of(world), "rsi")
    assert sorted((op.source, op.kind) for op in row.open_ops) == [
        ("deploy", "rollback"), ("switch", "bless")]
    assert row.issues == ("duplicate_open_op",)


# ---------------------------------------------------------------- 有用性

def _strategy_request(world, name, rsi, *, origin="staging", status="approved", pins=None,
                      deps="auto", kind=None):
    extra = dep("rsi", rsi.content_hash) if deps == "auto" else ""
    meta = world.deploy(name, "strategy", extra)
    return world.request(meta, status=status, origin=origin, inventory=(rsi,), deps=deps,
                         kind=kind)


def test_usefulness_counts_submissions_per_request_and_deployments_from_the_journal(world):
    rsi = world.deploy("rsi")
    first = _strategy_request(world, "s1", rsi)
    second = _strategy_request(world, "s2", rsi, status="rejected", origin="human")
    _strategy_request(world, "s3", rsi, status="pending")
    world.journal("s1", first, phase="decided")
    world.journal("s2", second, phase="reverted")      # decided まで進まない
    useful = by_name(model_of(world), "rsi").usefulness
    assert (useful.submitted, useful.deployed) == (3, 1)
    assert useful.by_pin == {rsi.content_hash: (3, 1)}


def test_reevaluation_requests_are_not_counted(world):
    rsi = world.deploy("rsi")
    request = _strategy_request(world, "s1", rsi, origin="reevaluation")
    world.journal("s1", request, phase="decided")
    useful = by_name(model_of(world), "rsi").usefulness
    assert (useful.submitted, useful.deployed) == (0, 0)


def test_non_strategy_requests_are_not_counted(world):
    rsi = world.deploy("rsi")
    meta = world.deploy("other", "indicator")
    world.request(meta, deps={"x": {"plugin": "rsi", "content_hash": rsi.content_hash}})
    world.request(meta, kind="signal", deps={"x": {"plugin": "rsi", "content_hash": "e" * 64}})
    useful = by_name(model_of(world), "rsi").usefulness
    assert useful.submitted == 0


def test_two_aliases_of_one_indicator_count_one_request(world):
    rsi = world.deploy("rsi")
    extra = (f"indicators:\n  fast: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n"
             f"  slow: {{plugin: rsi, pin: '{rsi.content_hash}', params: {{period: 28}}}}\n")
    meta = world.deploy("s1", "strategy", extra)
    request = world.request(meta, status="approved", inventory=(rsi,))
    world.journal("s1", request, phase="decided")
    useful = by_name(model_of(world), "rsi").usefulness
    assert (useful.submitted, useful.deployed) == (1, 1)


def test_requests_without_indicator_deps_are_unknown_not_zero(world):
    rsi = world.deploy("rsi")
    _strategy_request(world, "old", rsi, deps=None)             # key が無い古い依頼
    _strategy_request(world, "none", rsi, deps={})              # 依存なしと明記
    _strategy_request(world, "new", rsi)
    model = model_of(world)
    assert model.unknown_submissions == 1
    assert by_name(model, "rsi").usefulness.submitted == 1


def test_pins_that_differ_are_broken_down(world):
    rsi = world.deploy("rsi")
    meta = world.deploy("s1", "strategy", dep("rsi", rsi.content_hash))
    world.request(meta, status="approved", inventory=(rsi,))
    other = world.deploy("s2", "strategy", dep("rsi", rsi.content_hash))
    world.request(other, status="approved", deps={"a": {"plugin": "rsi",
                                                        "content_hash": "f" * 64}})
    useful = by_name(model_of(world), "rsi").usefulness
    assert useful.submitted == 2
    assert useful.by_pin == {rsi.content_hash: (1, 0), "f" * 64: (1, 0)}


# ---------------------------------------------------------------- pending と issue

def test_pending_hash_and_broken_pending_requests(world):
    meta = world.deploy("rsi")
    world.request(meta)
    world.raw_request("not json")
    world.raw_request(json.dumps({"name": "x", "content_hash": "zz"}))
    world.raw_request("not json", status="approved")      # pending でなければ数えない
    model = model_of(world)
    row = by_name(model, "rsi")
    assert [item.content_hash for item in row.pending] == [meta.content_hash]
    assert model.invalid_pending == (2, 3)


def test_the_dto_has_allowlisted_closed_fields(world):
    rsi = world.deploy("rsi")
    request = world.request(rsi, status="approved")
    world.journal("rsi", request)
    dto = model_to_dto(model_of(world))
    assert set(dto) == {"sources", "plugins", "invalid_pending", "unknown_submissions"}
    assert dto["sources"] == {"live": "ok", "pending": "ok", "journal": "ok"}
    assert set(dto["plugins"][0]) == {
        "name", "kind", "entity", "admission", "pending_count", "pending", "open_ops",
        "issues", "dependencies", "dependencies_set_state", "dependents", "usefulness"}
    detail = detail_to_dto(model_of(world), "rsi")
    assert set(detail) == {"sources", "plugin", "unknown_submissions"}
    assert {"history", "history_total", "versions"} <= set(detail["plugin"])
    assert detail_to_dto(model_of(world), "nothing") is None


@pytest.mark.parametrize("bad", [["staging"], {"a": 1}, 7, True])
def test_a_payload_field_of_the_wrong_type_is_dropped_not_fatal(world, bad):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    base = {"name": "mix", "content_hash": "a" * 64, "artifact_hash": "b" * 64}
    world.raw_request({**base, "kind": "strategy", "candidate_origin": bad})
    world.raw_request({**base, "kind": bad, "candidate_origin": "human"})
    world.raw_request({**base, "kind": "strategy", "candidate_origin": "human",
                       "mission_id": bad})
    model = model_of(world)
    row = by_name(model, "mix")
    assert [(item.origin, item.kind) for item in sorted(row.history, key=lambda i: i.id)] == [
        (None, "strategy"), ("human", None), ("human", "strategy")]
    assert by_name(model, "rsi") is not None
    dto = detail_to_dto(model, "mix")
    assert dto["plugin"]["history"][0]["origin"] == "human"


def test_the_history_is_cut_to_the_limit_and_the_dto_says_how_many_there_are(world):
    rsi = world.deploy("rsi")
    for _ in range(read_model.HISTORY_LIMIT + 7):
        world.request(rsi)
    model = model_of(world)
    row = by_name(model, "rsi")
    assert len(row.history) == read_model.HISTORY_LIMIT
    assert row.history_total == read_model.HISTORY_LIMIT + 7
    detail = detail_to_dto(model, "rsi")["plugin"]
    assert len(detail["history"]) == read_model.HISTORY_LIMIT
    assert detail["history_total"] == read_model.HISTORY_LIMIT + 7
    assert "history_total" not in model_to_dto(model)["plugins"][0]


def test_a_name_with_control_characters_is_cleaned_in_the_dto(world):
    world.raw_request(json.dumps({"name": "evil\nname\x1b[31m", "kind": "strategy",
                                  "content_hash": "a" * 64}))
    dto = model_to_dto(model_of(world))
    names = [row["name"] for row in dto["plugins"]]
    assert all("\n" not in name and "\x1b" not in name for name in names)


# ---------------------------------------------------------------- loader の経路は変わらない

def test_the_row_based_approval_rule_equals_the_db_reader(world):
    first, second = world.deploy("rsi"), world.deploy("adx")
    world.request(first, status="approved")
    world.request(first, status="rejected")
    world.request(second, status="approved")
    world.request(second, status="expired")
    world.request(second, status="pending")
    world.raw_request("not json", status="approved")
    world.raw_request(json.dumps(["list"]), status="approved")
    rows = world.conn.execute(
        "SELECT id,status,decided_at,payload_json FROM approval_requests").fetchall()
    assert plugin_loader.approved_hashes_from_rows(rows) == plugin_loader._approved_hashes_by_name(
        world.conn) == {"adx": {second.content_hash}}


def test_admit_still_excludes_with_the_resolver_reason_and_raises_nothing(world, caplog):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    bad = world.deploy("bad", "strategy", dep("rsi", "a" * 64, alias="fast"))
    world.request(bad, status="approved", inventory=(rsi,))
    result = plugin_loader.approved_plugins(world.conn, world.plugins, settings=SETTINGS)
    assert [(item.name, item.alias, item.reason) for item in result.rejected_strategies] == [
        ("bad", "fast", "pin_mismatch")]
    assert {meta.name for meta in result.inventory.metas} == {"rsi"}
    assert "indicator_unresolved:fast:pin_mismatch" in caplog.text


# ---------------------------------------------------------------- 空名・開いた op・読めない理由

@pytest.mark.parametrize("name", ["", "Bad Name", "../x"])
def test_a_pending_request_with_an_empty_or_ungrammatical_name_is_an_invalid_pending(world, name):
    approval = world.raw_request(
        {"name": name, "content_hash": "a" * 64, "kind": "strategy"})
    model = model_of(world)
    assert model.invalid_pending == (approval,)


def test_the_open_rule_is_the_deploy_store_rule(world):
    from agentic_fx.store import plugin_deploy_ops
    world.deploy_op("rsi", op_kind="rollback_symlink", phase="done")
    assert by_name(model_of(world), "rsi").open_ops == ()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plugin_deploy_ops, "is_open", lambda row: row["phase"] == "done")
        assert len(by_name(model_of(world), "rsi").open_ops) == 1


@pytest.mark.parametrize("case,reason", [
    ("missing_files", "missing_files"), ("invalid_symlink", "invalid_symlink"),
    ("non_canonical_name", "non_canonical_name"),
])
def test_the_issue_of_an_unreadable_entity_carries_its_own_reason(world, case, reason):
    name = "Bad-Name" if case == "non_canonical_name" else "victim"
    directory = world.plugins / name
    if case == "missing_files":
        directory.mkdir()
        (directory / "config.yaml").write_text("kind: indicator\n")
    elif case == "invalid_symlink":
        os.symlink("../../etc", directory)
    else:
        directory.mkdir()
    row = by_name(model_of(world), name)
    assert row.entity.reason == reason
    assert row.issues == (f"broken:{reason}",)


def test_the_approval_rows_are_read_by_column_not_with_a_star(world):
    statements = []
    world.conn.set_trace_callback(statements.append)
    try:
        model_of(world)
    finally:
        world.conn.set_trace_callback(None)
    mine = [text for text in statements if "approval_requests" in text]
    assert mine and not any("*" in text.split("FROM")[0] for text in mine
                            if text.lstrip().upper().startswith("SELECT"))
    assert not any("reason" in text.split("FROM")[0] or "decided_by" in text.split("FROM")[0]
                   for text in mine)


def test_a_version_folder_that_is_a_link_outside_the_plugins_root_is_not_readable(world, tmp_path):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    strategy = world.deploy("mom", "strategy", dep("rsi", rsi.content_hash))
    world.request(strategy, status="approved", inventory=(rsi,))
    healthy = by_name(model_of(world), "mom")
    assert [item.state for item in healthy.deps] != ["not_found"]
    matched = [item.state for item in healthy.deps]
    folder = world.plugins / ".versions" / "rsi"
    outside = tmp_path / "outside"
    folder.rename(outside)
    os.symlink(outside, folder)
    model = model_of(world)
    row = by_name(model, "rsi")
    assert row.entity.state == ENTITY_UNREADABLE and row.entity.reason == "invalid_symlink"
    assert row.admission.state == ADMISSION_NONE
    assert [item.state for item in by_name(model, "mom").deps] != matched
    assert row.issues == ("broken:invalid_symlink",)

"""質検査の母集団と判定 (ロジック等価・仮説族)。判定の反転・母集団の取り違え
を、正負ペアの fixture で拾う。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.loops import improve_quality as quality
from agentic_fx.store import candidate_archives as archives
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "candidate_quality"
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"

SMA_A = FIXTURES / "sma_cross_10_30"
RSI_A = FIXTURES / "rsi_bb_reversion_30_60"
RSI_B = FIXTURES / "rsi_bb_reversion_40_80"


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "t.db")
    init_db(db)
    yield db
    db.close()


def _live_root(tmp_path: Path, **plain: Path) -> Path:
    """名前 → 元ディレクトリの plain dir を置いた plugins root。"""
    import shutil
    root = tmp_path / "plugins"
    root.mkdir(exist_ok=True)
    for name, source in plain.items():
        shutil.copytree(source, root / name)
    return root


def _link_live(root: Path, name: str, source: Path, *, version="b") -> str:
    """版 dir を置いて live を正規形の symlink にする。symlink の指す先を返す。"""
    import shutil
    target = f".versions/{name}/{version * 64}"
    shutil.copytree(source, root / target)
    (root / name).symlink_to(target)
    return target


def _deployed_of(tmp_path: Path, name: str, source: Path) -> quality.Population:
    base = tmp_path / f"live_{name}"
    base.mkdir(exist_ok=True)
    return quality.load_live_deployed(_live_root(base, **{name: source}))[0]


def _row(conn, *, mission_id, name, logic_hash, outcome, family_key=None):
    row_id = archives.insert(
        conn, mission_id=mission_id, name=name, content_hash=f"c-{name}-{mission_id}",
        artifact_hash=f"a-{name}-{mission_id}", archive_path=None,
        pair="USDJPY", metrics={}, now=NOW, logic_hash=logic_hash,
        family_key=family_key, commit=True)
    conn.execute("UPDATE candidate_archives SET outcome_class=? WHERE id=?",
                 (outcome, row_id))
    conn.commit()
    return row_id


def _hash_of(path: Path) -> str:
    from agentic_fx.plugin.logic_identity import identity_of_dir
    return identity_of_dir(path, name="x")[0]


def _entry(source, name, logic_hash, *, kind="strategy", family=None,
           outcome=None):
    return quality.IdentityEntry(source, name, kind, logic_hash, family, outcome)


def _population(*entries):
    return quality.Population(entries=list(entries), attempted=len(entries))


# --- AC-20 (i) 判定の反転 ------------------------------------------------

def test_ac20_i_same_logic_hash_under_another_name_is_a_duplicate():
    deployed = _population(_entry("deployed", "alpha", "v1:" + "a" * 64))
    hit = quality.find_duplicate_logic(
        name="beta", logic_hash="v1:" + "a" * 64, populations=[deployed])
    assert hit is not None and hit.name == "alpha"


def test_ac20_i_one_statement_different_logic_hash_passes():
    deployed = _population(_entry("deployed", "alpha", "v1:" + "a" * 64))
    assert quality.find_duplicate_logic(
        name="beta", logic_hash="v1:" + "b" * 64, populations=[deployed]) is None


# --- AC-20 (iv) / AC-9 同名の例外 -----------------------------------------

def test_ac20_iv_ac9_same_name_deployed_old_version_is_an_update():
    deployed = _population(_entry("deployed", "alpha", "v1:" + "a" * 64))
    assert quality.find_duplicate_logic(
        name="alpha", logic_hash="v1:" + "a" * 64, populations=[deployed]) is None


def test_ac20_iv_other_name_deployed_is_a_duplicate_even_with_update_elsewhere():
    deployed = _population(
        _entry("deployed", "alpha", "v1:" + "a" * 64),
        _entry("deployed", "gamma", "v1:" + "a" * 64))
    hit = quality.find_duplicate_logic(
        name="alpha", logic_hash="v1:" + "a" * 64, populations=[deployed])
    assert hit is not None and hit.name == "gamma"


def test_ac9_same_name_approved_archive_row_is_allowed_only_if_the_name_is_deployed():
    approved = _entry("archive", "alpha", "v1:" + "a" * 64, outcome="approved")
    deployed = _population(_entry("deployed", "alpha", "v1:" + "c" * 64))
    archive = _population(approved)
    assert quality.find_duplicate_logic(
        name="alpha", logic_hash="v1:" + "a" * 64,
        populations=[deployed, archive]) is None
    assert quality.find_duplicate_logic(
        name="alpha", logic_hash="v1:" + "a" * 64,
        populations=[_population(), archive]) is not None


def test_ac12_same_name_rejected_row_is_still_a_duplicate():
    rejected = _entry("archive", "alpha", "v1:" + "a" * 64,
                      outcome="rejected_by_human")
    deployed = _population(_entry("deployed", "alpha", "v1:" + "c" * 64))
    assert quality.find_duplicate_logic(
        name="alpha", logic_hash="v1:" + "a" * 64,
        populations=[deployed, _population(rejected)]) is not None


def test_example_with_the_same_logic_is_a_duplicate_even_under_its_name():
    example = _entry("example", "sma_cross", "v1:" + "a" * 64)
    assert quality.find_duplicate_logic(
        name="sma_cross", logic_hash="v1:" + "a" * 64,
        populations=[_population(example)]) is not None


# --- AC-12 / AC-20 (ii) 母集団 ---------------------------------------------

@pytest.mark.parametrize("outcome", [
    "rejected_by_human", "duplicate_logic", "duplicate_metrics",
    "family_redirect", "revoked", "approved", "pending"])
def test_ac12_ac20_ii_decided_rows_of_other_missions_enter_the_population(
        conn, outcome):
    _row(conn, mission_id=1, name="old", logic_hash="v1:" + "a" * 64,
         outcome=outcome)
    population = quality.load_archive(conn, mission_id=2)
    assert [e.name for e in population.entries] == ["old"]
    hit = quality.find_duplicate_logic(
        name="new", logic_hash="v1:" + "a" * 64, populations=[population])
    assert hit is not None


def test_ac12_ac20_ii_rows_of_the_same_mission_are_not_compared(conn):
    _row(conn, mission_id=2, name="earlier_iteration",
         logic_hash="v1:" + "a" * 64, outcome="rejected_by_human")
    population = quality.load_archive(conn, mission_id=2)
    assert population.entries == [] and population.attempted == 0
    assert quality.find_duplicate_logic(
        name="new", logic_hash="v1:" + "a" * 64, populations=[population]) is None


@pytest.mark.parametrize("outcome", [
    "not_submitted", "unprofitable", "insufficient_trades", "gate_failed",
    "noop_copy", "expired", "invalidated"])
def test_undecided_or_floor_failed_rows_do_not_block_a_retry(conn, outcome):
    _row(conn, mission_id=1, name="old", logic_hash="v1:" + "a" * 64,
         outcome=outcome)
    population = quality.load_archive(conn, mission_id=2)
    assert population.entries == []


def test_rows_without_a_logic_hash_are_left_out_without_counting(conn):
    _row(conn, mission_id=1, name="legacy", logic_hash=None,
         outcome="rejected_by_human")
    population = quality.load_archive(conn, mission_id=2)
    assert population.entries == [] and population.attempted == 0
    assert population.skipped == []


# --- 比較対象 1 行の破損 ---------------------------------------------------

def test_ac18_a_corrupted_archive_row_is_skipped_and_reported(conn):
    bad = _row(conn, mission_id=1, name="broken", logic_hash="garbage",
               outcome="rejected_by_human")
    _row(conn, mission_id=1, name="good", logic_hash="v1:" + "a" * 64,
         outcome="rejected_by_human")
    population = quality.load_archive(conn, mission_id=2)
    assert [e.name for e in population.entries] == ["good"]
    assert population.skipped == [f"archive:{bad}"]
    assert not quality.base_unavailable(population)


def test_ac18_an_unreadable_deployed_dir_is_skipped_and_reported(tmp_path):
    root = _live_root(tmp_path, rsi_bb=RSI_A)
    ghost = root / "ghost"
    ghost.mkdir()
    (ghost / "plugin.py").write_text("x = 1\n")
    population, _ = quality.load_live_deployed(root)
    assert [e.name for e in population.entries] == ["rsi_bb"]
    assert population.skipped == ["deployed:ghost"]


def test_ac18_base_unavailable_only_when_something_was_attempted_and_nothing_usable(
        tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "ghost").symlink_to(f".versions/ghost/{'b' * 64}")
    nothing_usable, _ = quality.load_live_deployed(root)
    assert quality.base_unavailable(nothing_usable)
    assert not quality.base_unavailable(quality.Population())
    assert not quality.base_unavailable(
        nothing_usable, _deployed_of(tmp_path, "rsi_bb", RSI_A))


def test_ac18_database_read_failure_propagates(conn):
    conn.execute("DROP TABLE candidate_archives")
    with pytest.raises(sqlite3.Error):
        quality.load_archive(conn, mission_id=1)


# --- 仮説族 (c) AC-11 / AC-20 (iii) -----------------------------------------

def _family_of(path):
    from agentic_fx.plugin.logic_identity import identity_of_dir
    return identity_of_dir(path, name="x")[1]


def test_ac11_ac20_iii_family_of_a_deployed_other_name_is_detected(tmp_path):
    family = _family_of(RSI_B)
    deployed = _deployed_of(tmp_path, "rsi_bb_reversion", RSI_A)
    hit = quality.find_same_family(
        name="rsi6_bb200", kind="strategy", family_key=family, deployed=deployed)
    assert hit is not None and hit.name == "rsi_bb_reversion"


def test_ac11_same_name_is_an_update_not_a_family_hit(tmp_path):
    deployed = _deployed_of(tmp_path, "rsi_bb_reversion", RSI_A)
    assert quality.find_same_family(
        name="rsi_bb_reversion", kind="strategy", family_key=_family_of(RSI_B),
        deployed=deployed) is None


def test_ac20_iii_a_rejected_family_is_not_detected(conn):
    """却下済みの族は台帳にだけあり、配備済みの母集団には入らない。"""
    _row(conn, mission_id=1, name="rejected_one", logic_hash="v1:" + "a" * 64,
         outcome="rejected_by_human", family_key=_family_of(RSI_A))
    archive = quality.load_archive(conn, mission_id=2)
    assert archive.entries[0].family_key == _family_of(RSI_A)
    assert quality.find_same_family(
        name="new", kind="strategy", family_key=_family_of(RSI_B),
        deployed=quality.Population()) is None
    # 台帳由来の行は kind と族が一致していても、source が deployed でなければ
    # 配備済みの族として数えない
    same_kind = quality.Population(entries=[
        quality.IdentityEntry(
            "archive", "rejected_one", "strategy", "v1:" + "a" * 64,
            _family_of(RSI_A), "rejected_by_human")])
    assert quality.find_same_family(
        name="new", kind="strategy", family_key=_family_of(RSI_B),
        deployed=same_kind) is None


def test_ac11_family_is_compared_within_the_same_kind_only():
    deployed = quality.Population(
        entries=[_entry("deployed", "ind", "v1:" + "a" * 64, kind="indicator",
                        family="strategy:bollinger,rsi@1h@USDJPY")])
    assert quality.find_same_family(
        name="new", kind="strategy",
        family_key="strategy:bollinger,rsi@1h@USDJPY", deployed=deployed) is None


def test_example_without_a_family_never_matches(tmp_path):
    deployed = _deployed_of(tmp_path, "sma_cross", EXAMPLES / "sma_cross")
    assert quality.find_same_family(
        name="x", kind="strategy", family_key=None, deployed=deployed) is None


# --- 実ファイル: 母集団の読み込み -------------------------------------------

def test_deployed_population_carries_hash_family_and_kind_from_real_files(
        tmp_path):
    population = _deployed_of(tmp_path, "sma_cross_10_30", SMA_A)
    (entry,) = population.entries
    assert entry.source == "deployed" and entry.kind == "strategy"
    assert entry.family_key == "strategy:sma@1h@USDJPY"
    assert entry.logic_hash == _hash_of(SMA_A)


def test_examples_population_reads_every_example_directory():
    population = quality.load_examples(EXAMPLES)
    names = {e.name for e in population.entries}
    assert {"sma_cross", "rsi_indicator"} <= names
    assert population.skipped == []
    assert quality.load_examples(None).attempted == 0
    assert quality.load_examples(EXAMPLES / "missing").attempted == 0


# --- 配備済み母集団は live のファイルの状態が正 -------------------------------

def _decided_deploy(conn, root, name, source: Path, *, version="b"):
    """版 dir と live の symlink を置き、承認の切替が完了した記録を残す。"""
    from agentic_fx.store import plugin_switch_journal as journal
    target = _link_live(root, name, source, version=version)
    op_id = journal.insert(
        conn, kind="approve", approval_id=1, name=name, old_kind="absent",
        old_target=None, temp_path="t", new_target=target,
        switch_required=True, actor="t", now=NOW)
    assert journal.set_phase(conn, op_id, "decided", now=NOW)
    return target


def _closed_deploy_op(conn, *, op_kind, name, to_target=None, retired=None):
    from agentic_fx.store import plugin_deploy_ops as ops
    kwargs = dict(
        op_kind=op_kind, name=name, from_kind="symlink",
        to_kind={"disable_symlink": "absent", "rollback_symlink": "symlink"}[
            op_kind],
        from_target=f".versions/{name}/{'a' * 64}", to_target=to_target,
        retired_path=retired, subject_content_hash="c" * 64,
        subject_artifact_hash="a" * 64,
        target_content_hash="d" * 64 if to_target else None,
        target_artifact_hash="e" * 64 if to_target else None,
        approval_id=None, revoke=False, revoke_row_id=None, force=False,
        actor="t", now=NOW)
    op_id = ops.insert_intent(conn, **kwargs)
    assert ops.set_phase(conn, op_id, "done", now=NOW)


def _population_of(conn, root):
    file_population = quality.load_file_population(root, None)
    assert file_population.unavailable is None
    return quality.deployed_from_live(conn, file_population)


def test_live_symlink_is_read_through_its_version_directory(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    _link_live(root, "alpha", SMA_A)
    population, state = quality.load_live_deployed(root)
    assert [(e.name, e.logic_hash) for e in population.entries] == [
        ("alpha", _hash_of(SMA_A))]
    assert state == {"alpha": f".versions/alpha/{'b' * 64}"}


def test_a_symlink_outside_the_canonical_form_makes_the_live_state_unreadable(
        tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "alpha").symlink_to(outside)
    with pytest.raises(quality.LiveStateUnreadable):
        quality.load_live_deployed(root)
    result = quality.load_file_population(root, None)
    assert result.unavailable == (
        "file_population_read_failed:LiveStateUnreadable")


def test_a_directly_placed_plugin_after_a_disable_stays_in_the_population(
        conn, tmp_path):
    """disable の記録 (absent) の後に人が同名を直接置いても、live が正。
    食い違いは人の直接操作として報告される。"""
    root = _live_root(tmp_path)
    _decided_deploy(conn, root, "alpha", SMA_A)
    _closed_deploy_op(conn, op_kind="disable_symlink", name="alpha")
    (root / "alpha").unlink()
    import shutil
    shutil.copytree(RSI_A, root / "alpha")
    population, drift, why = _population_of(conn, root)
    assert why is None and drift == ["alpha"]
    assert [e.logic_hash for e in population.entries] == [_hash_of(RSI_A)]


def test_a_recorded_plugin_replaced_by_hand_is_read_as_its_new_version(
        conn, tmp_path):
    root = _live_root(tmp_path)
    _decided_deploy(conn, root, "alpha", SMA_A)
    (root / "alpha").unlink()
    import shutil
    shutil.copytree(RSI_A, root / "alpha")
    population, drift, _ = _population_of(conn, root)
    assert drift == ["alpha"]
    assert [e.logic_hash for e in population.entries] == [_hash_of(RSI_A)]


def test_a_live_state_that_matches_the_records_reports_no_drift(
        conn, tmp_path):
    root = _live_root(tmp_path)
    _decided_deploy(conn, root, "alpha", SMA_A)
    _decided_deploy(conn, root, "beta", RSI_A)
    (root / "beta").unlink()
    _closed_deploy_op(conn, op_kind="disable_symlink", name="beta")
    population, drift, why = _population_of(conn, root)
    assert why is None and drift == []
    assert [e.name for e in population.entries] == ["alpha"]


def test_a_plugin_removed_by_hand_is_reported_as_drift_and_absent(
        conn, tmp_path):
    root = _live_root(tmp_path)
    _decided_deploy(conn, root, "alpha", SMA_A)
    (root / "alpha").unlink()
    population, drift, _ = _population_of(conn, root)
    assert drift == ["alpha"] and population.entries == []


def test_names_without_a_deploy_record_are_read_from_live_without_drift(
        conn, tmp_path):
    root = _live_root(tmp_path, manual=SMA_A)
    population, drift, why = _population_of(conn, root)
    assert why is None and drift == []
    assert [e.name for e in population.entries] == ["manual"]


def test_an_open_switch_or_deploy_operation_makes_the_population_unavailable(
        conn, tmp_path):
    from agentic_fx.store import plugin_switch_journal as journal
    journal.insert(
        conn, kind="approve", approval_id=1, name="alpha", old_kind="absent",
        old_target=None, temp_path="t", new_target=".versions/alpha/x",
        switch_required=True, actor="t", now=NOW)
    population, drift, why = _population_of(conn, _live_root(tmp_path))
    assert why == "deploy_operation_in_flight" and population.entries == []


def test_load_file_population_turns_a_read_failure_into_unavailable(tmp_path):
    examples = tmp_path / "ex"
    examples.mkdir()
    from unittest.mock import patch
    with patch.object(Path, "iterdir", side_effect=PermissionError(13, "x")):
        result = quality.load_file_population(tmp_path / 'plugins', examples)
    assert result.unavailable == "file_population_read_failed:PermissionError"
    assert result.deployed.entries == [] and result.examples.entries == []

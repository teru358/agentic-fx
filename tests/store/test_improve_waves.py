"""improve_waves / improve_wave_slots CRUD (設計書 §3.1)。

**Task 8 が保証する範囲**: wave+slot の 1 tx 作成、slot の CAS 遷移
(reserved->claimed->running->done|failed)、pre-ready 失敗時の
claimed->reserved 巻き戻し (spawn_attempts 管理)、終端直積の SQL。
`go` の実プロトコル (WorkerRunner との handshake) は Task 9 が別途組む。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.store import improve_waves, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)  # Sat 03:00


def _mission(c):
    """裁定 D1: `improve_wave_slots.mission_id` に FK が付いたため、
    実在する missions 行の id を使う必要がある。"""
    return missions.start(c, "improve", "local", "m", now=NOW)


def test_create_wave_and_slots_in_one_tx_rowcount_one_means_authority(tmp_path):
    """1 つの短い tx で wave 行 + slot 行 (reserved) を作る。
    `INSERT OR IGNORE` の rowcount=1 が起動権 (period 消費の証)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    created = improve_waves.create_wave_and_slots(
        c, period_key="2026-W34", now=NOW, expected=2)
    assert created is True
    slots = improve_waves.list_slots(c, period_key="2026-W34")
    assert [s["k"] for s in slots] == [0, 1]
    assert all(s["status"] == "reserved" for s in slots)


def test_create_wave_and_slots_second_call_is_noop_period_already_consumed(tmp_path):
    """同一 period_key への 2 回目の呼び出しは False (wave 行が既に在る =
    period 消費済み。実行 0 件でも次 tick で再 CAS しない)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=2)
    second = improve_waves.create_wave_and_slots(
        c, period_key="2026-W34", now=NOW, expected=4)  # M が違っても無視
    assert second is False
    slots = improve_waves.list_slots(c, period_key="2026-W34")
    assert len(slots) == 2  # 最初の M=2 のまま


def test_create_wave_and_slots_m_zero_writes_nothing(tmp_path):
    """M=0 なら何も書かない (period 非消費、次 tick で再試行)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    created = improve_waves.create_wave_and_slots(
        c, period_key="2026-W34", now=NOW, expected=0)
    assert created is False
    row = c.execute("SELECT 1 FROM improve_waves WHERE period_key=?",
                    ("2026-W34",)).fetchone()
    assert row is None  # wave 行すら作らない


def test_claim_slot_cas_reserved_to_claimed_with_mission_id(tmp_path):
    """Tx-0 と同じ tx で呼ばれる CAS: reserved -> claimed。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    mid = _mission(c)
    ok = improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=mid, now=NOW)
    assert ok is True
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "claimed"
    assert slot["mission_id"] == mid
    assert slot["spawn_attempts"] == 1


def test_claim_slot_cas_fails_on_non_reserved(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    ok = improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    assert ok is False


def test_mark_running_cas_claimed_to_running(tmp_path):
    """`ready` 受信後、親が短い tx で claimed->running を commit する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    ok = improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is True
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "running"


def test_revert_to_reserved_on_pre_ready_failure_clears_mission_id(tmp_path):
    """§3.1⑥・codex 12周目I3: 戻すときは同じ tx で mission_id=NULL も戻す。
    再試行上限の判定は Task 9 が行う (`revert_to_reserved` は無条件遷移)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.revert_to_reserved(c, period_key="2026-W34", k=0, now=NOW)
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "reserved"
    assert slot["mission_id"] is None
    assert slot["spawn_attempts"] == 1  # claim 時の +1 のみ (revert 自体は増やさない)


def test_revert_to_reserved_fails_on_non_claimed(tmp_path):
    """CAS: claimed 以外からは遷移しない (rowcount=0)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    ok = improve_waves.revert_to_reserved(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is False
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "reserved"


def test_mark_slot_failed_after_attempts_exhausted(tmp_path):
    """Task 9 が spawn_attempts >= max と判定した後に呼ぶ想定の failed 遷移。
    claimed からでも running からでも failed へ落とし mission_id を NULL に戻す。
    **裁定 D2 (2026-08-24)**: `reserved`/`claimed`/`running` からの CAS
    (以前の「無条件」ではない — `test_mark_slot_failed_does_not_overwrite_done_slot`
    参照)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    m1, m2 = _mission(c), _mission(c)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=m1, now=NOW)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=m2, now=NOW)  # 失敗 (reserved でない) — 無視
    improve_waves.revert_to_reserved(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=m2, now=NOW)
    slot_before_fail = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot_before_fail["spawn_attempts"] == 2  # L08: 定数化変異 (=1) の pin
    ok = improve_waves.mark_slot_failed(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is True
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "failed"
    assert slot["mission_id"] is None


def test_mark_slot_failed_does_not_overwrite_done_slot(tmp_path):
    """裁定 D2 (2026-08-24) killer: 終端済み (`done`) の slot に
    `mark_slot_failed` を当てても上書きされない (CAS `False`)。
    以前の「無条件」実装なら `done -> failed` に書き換わっていた。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0, status="done", now=NOW)
    ok = improve_waves.mark_slot_failed(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is False
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"


def test_count_open_slots_counts_reserved_only(tmp_path):
    """`count_open_slots` は `reserved` の数を返す (claimed/running/done/failed は含まない)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=3)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    assert improve_waves.count_open_slots(c, period_key="2026-W34") == 2
    improve_waves.claim_slot(c, period_key="2026-W34", k=1, mission_id=_mission(c), now=NOW)
    assert improve_waves.count_open_slots(c, period_key="2026-W34") == 1


def test_mark_terminal_running_to_done(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0, status="done", now=NOW)
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"


# precheck 2026-08-22: T8-m6 — parametrize が本体で未使用だった (2 回とも同じ検査)。
# `terminal_status` を実際に使い、有効な終端状態を経由してから無効値を渡す形に直す。
@pytest.mark.parametrize("terminal_status", ["done", "failed"])
def test_mark_terminal_rejects_invalid_status(tmp_path, terminal_status):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0,
                                status=terminal_status, now=NOW)
    with pytest.raises(ValueError):
        improve_waves.mark_terminal(c, period_key="2026-W34", k=0,
                                    status="not_a_status", now=NOW)


def test_recover_stale_slots_fails_reserved_claimed_running(tmp_path):
    """§8.1-20: 再起動時に reserved (mission_id IS NULL を assert)・claimed・
    running を全て failed へ収束する SQL。手動 one-shot (slot 無し) は対象外。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=3)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)  # claimed
    improve_waves.claim_slot(c, period_key="2026-W34", k=1, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=1, now=NOW)                # running
    # k=2 は reserved のまま (mission_id IS NULL)

    n = improve_waves.recover_stale_slots(c, now=NOW)
    assert n == 3
    for k in (0, 1, 2):
        slot = improve_waves.get_slot(c, period_key="2026-W34", k=k)
        assert slot["status"] == "failed"


def test_recover_stale_slots_leaves_done_and_failed_untouched(tmp_path):
    """L-B3 是正 (束D検収, verified-local-round1.md §11 #21): テスト名は
    「done と failed の両方を untouched のまま残す」を謳うが、本体は
    `done` しか作っていなかった。`failed` slot を追加で作り、両方が
    `recover_stale_slots` の対象外のまま残ることを実測する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=2)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0, status="done", now=NOW)
    improve_waves.claim_slot(c, period_key="2026-W34", k=1, mission_id=_mission(c), now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=1, status="failed", now=NOW)
    n = improve_waves.recover_stale_slots(c, now=NOW)
    assert n == 0
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"
    assert improve_waves.get_slot(c, period_key="2026-W34", k=1)["status"] == "failed"

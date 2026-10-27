"""`prepare` の追加引数: 週次 claim の完了通知 (`on_claimed`) と一時指示。"""
from __future__ import annotations

import dataclasses

import pytest

from agentic_fx.loops.improve_loop import ImprovePrepareFailed
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store.db import connect_readonly
from tests.loops.conftest import _prepare_wave_slot

CANARY = "CANARY-ONEOFF-7f3a"


def _slot(conn, clock, key="2026-W34"):
    now = clock.now()
    _prepare_wave_slot(conn, period_key=key, k=0, now=now)
    backlog_store.add(conn, "idea-for-partition", "user", now)
    conn.commit()
    return now


def test_on_claimed_runs_after_the_claim_transaction_is_durable(
        loop_full, conn, clock, tmp_path):
    now = _slot(conn, clock)
    seen = []

    def on_claimed():
        fresh = connect_readonly(tmp_path / "t.db")
        try:
            seen.append(fresh.execute(
                "SELECT status FROM improve_wave_slots "
                "WHERE wave_period_key='2026-W34' AND k=0").fetchone()["status"])
        finally:
            fresh.close()

    loop_full.prepare(slot_key=("2026-W34", 0), now=now, on_claimed=on_claimed)
    assert seen == ["claimed"]


def test_on_claimed_is_not_called_when_the_claim_fails(loop_full, conn, clock):
    now = _slot(conn, clock)
    conn.execute("UPDATE improve_wave_slots SET status='failed' "
                 "WHERE wave_period_key='2026-W34'")
    conn.commit()
    calls = []
    with pytest.raises(RuntimeError):
        loop_full.prepare(slot_key=("2026-W34", 0), now=now,
                          on_claimed=lambda: calls.append(1))
    assert calls == []


def test_on_claimed_failure_compensates_like_any_post_claim_failure(
        loop_full, conn, clock):
    now = _slot(conn, clock)

    def boom():
        raise OSError("release failed")

    with pytest.raises(ImprovePrepareFailed):
        loop_full.prepare(slot_key=("2026-W34", 0), now=now, on_claimed=boom)
    assert conn.execute("SELECT count(*) c FROM missions WHERE status='running'"
                        ).fetchone()["c"] == 0


def test_oneoff_comment_reaches_the_prompt_and_is_not_kept(loop_full, conn, clock):
    now = clock.now()
    mission, ctx, _runner = loop_full.prepare(
        slot_key=None, now=now, oneoff_comment=CANARY)
    assert mission.prompt.count(f"- {CANARY}") == 1
    assert mission.prompt.count("## 今回だけの指示") == 1
    for f in dataclasses.fields(ctx):
        assert CANARY not in repr(getattr(ctx, f.name))
    assert CANARY not in loop_full._last_rendered_prompt


def test_without_a_comment_there_is_no_section(loop_full, conn, clock):
    mission, _ctx, _runner = loop_full.prepare(slot_key=None, now=clock.now())
    assert "今回だけの指示" not in mission.prompt

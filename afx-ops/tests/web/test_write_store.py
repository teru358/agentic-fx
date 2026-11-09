"""確認 record の状態機械: 結び付け・期限・使い捨て・件数の上限・並行・冪等 key。"""
from __future__ import annotations

import hashlib
import threading
import time

import pytest

from afx_ops.web import write
from afx_ops.web.write import (
    CONSUMED_LIMIT, CONSUMED_TTL, DONE, RUNNING, UNKNOWN, UNUSED_LIMIT, UNUSED_TTL,
    ConfirmationStore, Intent, Outcome, WriteReject,
)

from _write_support import DIGEST, Clock

APPROVE = Intent("approve", 7, DIGEST)


def _store():
    clock = Clock()
    return clock, ConfirmationStore(clock)


def _reject_status(function, *args) -> int:
    with pytest.raises(WriteReject) as info:
        function(*args)
    return info.value.status


def test_a_token_is_43_urlsafe_chars_and_each_one_is_new():
    _clock, store = _store()
    tokens = {store.create("afx", APPROVE) for _ in range(5)}
    assert len(tokens) == 5
    assert all(write.TOKEN_PATTERN.fullmatch(token) for token in tokens)


def test_the_token_is_not_kept_anywhere_in_the_store():
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    assert token not in repr(vars(store)) and token not in repr(store._records)
    assert all(token not in repr(vars(record)) for record in store._records.values())


def test_the_idempotency_key_is_a_fixed_prefix_and_the_sha256_of_the_token():
    token = "A" * 43
    key = write.idempotency_key_of(token)
    assert key == write.KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()
    assert token not in key


def test_a_token_is_consumable_once_and_carries_the_stored_intent():
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    snapshot = store.consume(token, "afx")
    assert snapshot.intent == APPROVE and snapshot.state == RUNNING
    assert _reject_status(store.consume, token, "afx") == 409


def test_an_unknown_or_malformed_token_is_refused():
    _clock, store = _store()
    assert _reject_status(store.consume, "A" * 43, "afx") == 403
    assert _reject_status(store.consume, "short", "afx") == 400
    assert _reject_status(store.consume, "A" * 42 + "!", "afx") == 400


def test_a_token_bound_to_another_user_is_refused_and_stays_usable_by_its_owner():
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    assert _reject_status(store.consume, token, "mallory") == 403
    assert store.consume(token, "afx").intent == APPROVE


def test_a_token_expires_after_ten_minutes():
    clock, store = _store()
    token = store.create("afx", APPROVE)
    clock.now += UNUSED_TTL - 0.001
    assert store.consume(token, "afx").state == RUNNING
    other = store.create("afx", APPROVE)
    clock.now += UNUSED_TTL
    assert _reject_status(store.consume, other, "afx") == 410


def test_the_unused_limit_refuses_the_33rd_and_keeps_the_existing_tokens():
    _clock, store = _store()
    tokens = [store.create("afx", APPROVE) for _ in range(UNUSED_LIMIT)]
    assert _reject_status(store.create, "afx", APPROVE) == 429
    assert store.counts() == (UNUSED_LIMIT, 0)
    assert store.consume(tokens[0], "afx").state == RUNNING
    assert store.consume(tokens[-1], "afx").state == RUNNING


def test_expired_unused_records_are_dropped_before_the_limit_is_judged():
    clock, store = _store()
    for _ in range(UNUSED_LIMIT):
        store.create("afx", APPROVE)
    clock.now += UNUSED_TTL
    assert store.create("afx", APPROVE)
    assert store.counts() == (1, 0)


def test_a_consumed_record_does_not_count_against_the_unused_limit():
    _clock, store = _store()
    first = store.create("afx", APPROVE)
    store.consume(first, "afx")
    for _ in range(UNUSED_LIMIT):
        store.create("afx", APPROVE)
    assert store.counts() == (UNUSED_LIMIT, 1)


def _consume_and_finish(store, state=DONE):
    token = store.create("afx", APPROVE)
    snapshot = store.consume(token, "afx")
    if state is not None:
        store.finish(snapshot.record_id, state, Outcome("applied", 200))
    return token, snapshot


def test_consumed_records_are_kept_up_to_32_and_the_oldest_finished_one_is_evicted():
    clock, store = _store()
    first_token, _ = _consume_and_finish(store)
    for _ in range(CONSUMED_LIMIT - 1):
        clock.now += 1
        _consume_and_finish(store)
    assert store.counts() == (0, CONSUMED_LIMIT)
    clock.now += 1
    _consume_and_finish(store)
    assert store.counts() == (0, CONSUMED_LIMIT)
    assert _reject_status(store.check, first_token, "afx") == 403


def test_a_running_record_is_never_evicted_and_a_full_running_table_refuses_without_consuming():
    clock, store = _store()
    running = [_consume_and_finish(store, None)[0] for _ in range(CONSUMED_LIMIT)]
    waiting = store.create("afx", APPROVE)
    assert _reject_status(store.consume, waiting, "afx") == 429
    assert store.counts() == (1, CONSUMED_LIMIT)
    assert _reject_status(store.consume, running[0], "afx") == 409
    # 1 件が終われば、待っていた token は消費されないまま使える
    snapshot_id = next(iter(store._records))
    store.finish(snapshot_id, DONE, Outcome("applied", 200))
    assert store.consume(waiting, "afx").state == RUNNING


def test_consumed_records_are_dropped_after_an_hour_unless_running():
    clock, store = _store()
    done, _ = _consume_and_finish(store)
    running, _ = _consume_and_finish(store, None)
    clock.now += CONSUMED_TTL - 1
    assert store.check(done, "afx").state == DONE
    clock.now += 1
    store.create("afx", APPROVE)
    assert _reject_status(store.check, done, "afx") == 403
    assert _reject_status(store.check, running, "afx") == 409


def test_finish_only_moves_a_running_record_and_only_to_a_final_state():
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    record_id = write.record_id_of(token)
    store.finish(record_id, DONE, Outcome("applied", 200))
    assert store.counts() == (1, 0)
    with pytest.raises(ValueError):
        store.finish(record_id, RUNNING, None)
    store.consume(token, "afx")
    store.finish(record_id, UNKNOWN, None)
    store.finish(record_id, DONE, Outcome("applied", 200))
    assert store.check(token, "afx").state == UNKNOWN


def test_check_refuses_unused_and_running_returns_done_as_is_and_reopens_only_an_unknown_improve():
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    assert _reject_status(store.check, token, "afx") == 409
    snapshot = store.consume(token, "afx")
    assert _reject_status(store.check, token, "afx") == 409
    store.finish(snapshot.record_id, DONE, Outcome("applied", 200))
    assert store.check(token, "afx").state == DONE
    assert store.check(token, "afx").state == DONE

    improve = Intent("improve", comment="x")
    token = store.create("afx", improve)
    snapshot = store.consume(token, "afx")
    store.finish(snapshot.record_id, UNKNOWN, None)
    assert store.check(token, "afx").state == UNKNOWN
    # 結果不明の改善は実行中に戻り、並行する 2 本目の check は通らない
    assert _reject_status(store.check, token, "afx") == 409

    decision = store.create("afx", APPROVE)
    snapshot = store.consume(decision, "afx")
    store.finish(snapshot.record_id, UNKNOWN, None)
    assert store.check(decision, "afx").state == UNKNOWN
    assert store.check(decision, "afx").state == UNKNOWN


def test_check_refuses_another_user():
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    snapshot = store.consume(token, "afx")
    store.finish(snapshot.record_id, DONE, Outcome("applied", 200))
    assert _reject_status(store.check, token, "mallory") == 403


def test_parallel_consumes_of_one_token_succeed_exactly_once(monkeypatch):
    _clock, store = _store()
    token = store.create("afx", APPROVE)
    original = store._consumed_count

    def slow():
        # 状態の判定と遷移の間に他の thread を割り込ませる
        time.sleep(0.02)
        return original()

    monkeypatch.setattr(store, "_consumed_count", slow)
    barrier = threading.Barrier(8)
    results: list[object] = []

    def attempt():
        barrier.wait()
        try:
            results.append(store.consume(token, "afx").state)
        except WriteReject as reject:
            results.append(reject.status)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(RUNNING) == 1
    assert results.count(409) == 7


def test_a_consumed_record_past_the_hour_is_refused_on_check_without_another_create():
    clock, store = _store()
    token = store.create("afx", Intent("improve", comment="x"))
    snapshot = store.consume(token, "afx")
    store.finish(snapshot.record_id, UNKNOWN, None)
    clock.now += CONSUMED_TTL - 1
    assert store.check(token, "afx").state == UNKNOWN
    store.finish(snapshot.record_id, UNKNOWN, None)
    clock.now += CONSUMED_TTL
    assert _reject_status(store.check, token, "afx") == 410
    assert _reject_status(store.check, token, "afx") == 403

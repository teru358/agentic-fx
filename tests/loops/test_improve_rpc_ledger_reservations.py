from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger


def _ledger() -> ImproveRpcLedger:
    return ImproveRpcLedger(rpc_timeout_sec_by_kind={})


def test_begin_end_reserves_only_while_open():
    ledger = _ledger()
    reservation = ledger.begin_accept()
    assert reservation == (0, 0)
    ledger.end_accept(reservation)
    ledger.freeze()
    assert ledger.begin_accept() is None


def test_freeze_waits_for_reservation_to_end():
    ledger = _ledger()
    reservation = ledger.begin_accept()
    barrier = threading.Barrier(2)
    finished = threading.Event()

    def freeze() -> None:
        barrier.wait()
        ledger.freeze(drain_timeout_sec=1.0)
        finished.set()

    thread = threading.Thread(target=freeze)
    thread.start()
    barrier.wait()
    assert not finished.wait(0.05)
    ledger.end_accept(reservation)
    thread.join(1.0)
    assert finished.is_set()


def test_freeze_timeout_drops_generation_and_late_end_is_noop():
    ledger = _ledger()
    old = ledger.begin_accept()
    dropped: list[int] = []
    ledger.freeze(drain_timeout_sec=0.0, on_timeout=dropped.append)
    assert dropped == [1]
    ledger.end_accept(old)
    assert ledger.entries() == []
    # 段 0 pin (2026-09-10): 旧 generation の遅延 end は予約数を動かさない
    # (generation 判定と `<= 0` ガードの二重防御。片方だけ落とすと等価だが
    # 両方落とすと負値になる)。内部カウンタを直接観測する。
    assert ledger._reservations == 0
    ledger.end_accept(old)
    assert ledger._reservations == 0


def test_freeze_if_open_is_idempotent():
    ledger = _ledger()
    ledger.freeze_if_open()
    ledger.freeze_if_open()
    assert ledger.entries() == []


@pytest.mark.parametrize(
    ("first", "second", "allowed"),
    [
        ("persist_failed", "persisted", True),
        ("persist_failed", "discarded", True),
        ("persisted", "discarded", False),
        ("discarded", "persisted", False),
        ("persisted", "persist_failed", False),
        ("discarded", "persist_failed", False),
    ],
)
def test_persist_failed_transition_table(first, second, allowed):
    ledger = _ledger()
    ledger.freeze()
    getattr(ledger, f"mark_{first}")()
    action = getattr(ledger, f"mark_{second}")
    if allowed:
        action()
    else:
        with pytest.raises(RuntimeError):
            action()


def test_freeze_timeout_advances_generation_for_new_reservations():
    """ローカル 1 周目 #1 (2026-09-10): drain timeout で世代を進めないと、
    timeout 後に取り直した予約が旧予約と同じ generation になり、旧予約の
    遅延 end_accept が新しい予約を減らしてしまう。既存 pin
    (`_reservations == 0`) は `<= 0` ガードに吸収され世代前進を見ていない。"""
    ledger = _ledger()
    old = ledger.begin_accept()
    ledger.freeze(drain_timeout_sec=0.0, on_timeout=lambda n: None)
    ledger._state = "OPEN"          # drop 後の世代だけを観測する
    fresh = ledger.begin_accept()
    assert fresh[0] != old[0]       # generation が進んでいる
    ledger.end_accept(old)          # 旧世代の遅延 end は新予約を減らさない
    assert ledger._reservations == 1


def test_reservation_tuple_is_generation_then_token():
    """ローカル 1 周目 #2 (2026-09-10): 予約の戻り値が (generation, token) の
    順であること。既存 `assert reservation == (0, 0)` は初期状態で両要素が 0
    なので順序反転を通す。token だけが進む 2 本目で順序を確定させる。"""
    ledger = _ledger()
    first = ledger.begin_accept()
    second = ledger.begin_accept()
    assert first == (0, 0)
    assert second == (0, 1)         # generation は据え置き、token が進む
    ledger.freeze(drain_timeout_sec=0.0, on_timeout=lambda n: None)
    ledger._state = "OPEN"
    third = ledger.begin_accept()
    assert third == (1, 2)          # generation が進み token も進む


def test_freeze_blocks_on_condition_instead_of_busy_spinning():
    """ローカル 1 周目 #3 (2026-09-10): `self._condition.wait(remaining)` を
    落とすと freeze は condition ロックを握ったまま busy spin し、drain 中の
    end_accept が deadline まで一切通らない。既存
    test_freeze_waits_for_reservation_to_end は join(1.0) が deadline (1.0) と
    同値なので通ってしまう。end_accept 自身が詰まらないことを pin する。"""
    ledger = _ledger()
    reservation = ledger.begin_accept()
    entered = threading.Event()

    def freeze() -> None:
        entered.set()
        ledger.freeze(drain_timeout_sec=5.0)

    thread = threading.Thread(target=freeze, daemon=True)
    thread.start()
    assert entered.wait(1.0)
    time.sleep(0.2)                 # freeze が drain 待機に入るのを待つ
    t0 = time.monotonic()
    ledger.end_accept(reservation)  # busy spin 実装ではロック待ちで詰まる
    elapsed = time.monotonic() - t0
    thread.join(1.0)
    assert elapsed < 1.0            # deadline 5.0 を待たされていない
    assert not thread.is_alive()    # end_accept で freeze が起きて完了した

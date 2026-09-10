from __future__ import annotations

import threading
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

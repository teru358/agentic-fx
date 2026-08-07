"""worker.py の rlimit 拡張 (プラン 8 B 束) の単体テスト。"""
from __future__ import annotations

import resource

import pytest

from agentic_fx.plugin.worker import _set_resource_limits


def test_set_resource_limits_sets_nofile_and_fsize():
    """RLIMIT_NOFILE/RLIMIT_FSIZE が指定値ちょうどに設定される。

    実プロセスの現在の rlimit を破壊しないよう、テスト後に元へ戻す
    (RLIMIT_CPU/RLIMIT_AS は既存 test_sandbox.py の実測パターンに揃え、
    ここでは NOFILE/FSIZE のみを対象にする — CPU/AS を実際にこのテスト
    プロセスへ適用すると pytest 自体の実行を壊しかねないため、資源制限は
    別プロセス (test_sandbox.py の実 subprocess テスト) で検証し、ここは
    「setrlimit が正しい引数で呼ばれること」を fake 経由で検証する)。
    """
    calls: list[tuple[int, tuple[int, int]]] = []

    def fake_setrlimit(which, limits):
        calls.append((which, limits))

    import agentic_fx.plugin.worker as worker_mod
    orig = resource.setrlimit
    try:
        worker_mod.resource.setrlimit = fake_setrlimit  # type: ignore[attr-defined]
        _set_resource_limits(cpu_sec=60, memory_mb=512, nofile=128, fsize_mb=8)
    finally:
        worker_mod.resource.setrlimit = orig

    kinds = {which for which, _ in calls}
    assert resource.RLIMIT_NOFILE in kinds
    assert resource.RLIMIT_FSIZE in kinds
    nofile_limit = next(v for w, v in calls if w == resource.RLIMIT_NOFILE)
    assert nofile_limit == (128, 128)
    fsize_limit = next(v for w, v in calls if w == resource.RLIMIT_FSIZE)
    assert fsize_limit == (8 * 1024 * 1024, 8 * 1024 * 1024)

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


def _drive_worker_main(monkeypatch, tmp_path, *, limits_fail: bool):
    import io
    import json

    from agentic_fx.plugin import worker

    events = []
    out = io.BytesIO()
    handshakes = [{"cpu_sec": 5, "memory_mb": 512, "nofile": 64,
                   "fsize_mb": 8, "kind": "indicator"}]

    def set_limits(*args):
        events.append("limits")
        if limits_fail:
            raise OSError("core disabled")

    def import_plugin(path):
        events.append("import")
        raise RuntimeError("stop after import")

    monkeypatch.setattr(worker.sys, "argv", ["worker", str(tmp_path)])
    monkeypatch.setattr(worker, "_protect_protocol_stdout", lambda: out)
    monkeypatch.setattr(worker, "_read_line",
                        lambda: handshakes.pop(0) if handshakes else None)
    monkeypatch.setattr(worker, "_set_resource_limits", set_limits)
    monkeypatch.setattr(worker, "_poison_network_modules", lambda: None)
    monkeypatch.setattr(worker, "_import_plugin", import_plugin)
    worker.main()
    reply = json.loads(out.getvalue().decode().splitlines()[0])
    return events, reply


def test_worker_main_sets_resource_limits_before_importing_the_plugin(
        monkeypatch, tmp_path):
    events, reply = _drive_worker_main(monkeypatch, tmp_path, limits_fail=False)
    assert events == ["limits", "import"]
    assert reply["ok"] is False and reply["ready"] is False


def test_worker_main_never_imports_the_plugin_when_a_limit_cannot_be_set(
        monkeypatch, tmp_path):
    events, reply = _drive_worker_main(monkeypatch, tmp_path, limits_fail=True)
    assert events == ["limits"]
    assert reply["ok"] is False and reply["ready"] is False


def test_validator_folds_list_to_series_before_wire():
    """validator は list を df_index 付きで必ず pd.Series に畳むので、
    wire 変換には畳まれた後の形で入り、NaN は null になる。"""
    import pandas as pd

    from agentic_fx.core.plugin_contract import validate_indicator_result
    from agentic_fx.plugin.worker import _indicator_result_to_wire

    idx = pd.RangeIndex(3)
    validated = validate_indicator_result(
        {"a": [1.0, float("nan"), 3.0]}, df_index=idx, outputs=("a",))
    assert isinstance(validated["a"], pd.Series)
    assert _indicator_result_to_wire(validated) == {
        "a": {"series": [1.0, None, 3.0]}}

"""`afx improve verify-backend` の one-shot protocol (§8.1-45)。

`WorkerRunner` を fake に差し替えたユニット/契約テスト — 実 CLI は呼ばない。
実機での実測は Task 13 の手動ランブック節と
`tests/loops/test_verify_backend_realbackend.py` (`@pytest.mark.realbackend`)
の対象 (本ファイルの対象外)。

<!-- precheck 2026-08-23 wave3: T13-B1 T13-B2 T13-B3 -->
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.verify_backend import (
    OPERATOR_SHELL_NO_ISOLATION, VerifyBackendGateError, VerifyBackendResult,
    selftest_detail_or_default, selftest_failure_detail, verify_backend as _verify_backend)
from agentic_fx.runners.base import Mission, MissionResult

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)

_BEHAVIOR: dict[str, dict] = {"current": {}}


def verify_backend(*args, **kwargs):
    """既存の WorkerRunner 契約試験を運用者 shell の明示経路で実行する。"""
    kwargs.setdefault("isolation_options", OPERATOR_SHELL_NO_ISOLATION)
    return _verify_backend(*args, **kwargs)


class _FakeVerifyWorkerRunner:
    """`WorkerRunner(root=/settings=/clock=/rag=/worker_profile=/run_context=/
    on_ready=/rpc_handlers=)` の契約だけを検査する替え玉。実サブプロセスは
    起動しない — `FakeImproveWorkerRunner` (Task 12) と同じ発想の注入点。
    `_BEHAVIOR["current"]["result"]` (mission -> MissionResult の callable)
    でテストごとに振る舞いを差し替える。"""
    captured_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs) -> None:
        _FakeVerifyWorkerRunner.captured_kwargs = kwargs
        self._on_ready = kwargs["on_ready"]
        self._ctx = kwargs["run_context"]

    def run(self, mission: Mission) -> MissionResult:
        behavior = _BEHAVIOR["current"]
        if not behavior.get("skip_ready"):
            # <!-- precheck 2026-08-23 R-D3 -->
            # 裁定 R-D3: 実 WorkerRunner は source_snapshot_dir (出所) を
            # そのまま echo せず、workdir/"source" (使い捨て workdir 配下)
            # を返す。この fake も同じ契約を模す — 実在しない固定パスの
            # basename だけを "source" に揃える (verify_backend 側の
            # on_ready ゲートは basename のみを照合する、下記参照)。
            run_context = {
                "mission_id": str(self._ctx.mission_id),
                "staging_dir": str(self._ctx.staging_dir),
                "source_snapshot_dir": str(Path("/fake-workdir") / "source")}
            # A17 是正 (束D検収, verified-local-round1.md §11 #6): 親ゲート
            # (a) の否定側 (mismatch) を注入できる seam。`bad_ready` に
            # フィールド名を渡すと、そのフィールドだけ壊れた値で echo する。
            bad_field = behavior.get("bad_ready")
            if bad_field == "mission_id":
                run_context["mission_id"] = "not-the-mission-id"
            elif bad_field == "staging_dir":
                run_context["staging_dir"] = "/not/the/staging/dir"
            elif bad_field == "source_snapshot_dir_basename":
                run_context["source_snapshot_dir"] = str(
                    Path("/fake-workdir") / "not-source")
            elif bad_field == "source_snapshot_dir_missing":
                # round2 最終是正 A9 (2026-08-29、verified-local-round2.md
                # A9): `or echoed_source is None` を落とす変異は、値が違う
                # (basename mismatch) frame しか流していない既存 parametrize
                # では殺せない — キー自体が無い frame を注入する。
                del run_context["source_snapshot_dir"]
            self._on_ready({"type": "ready", "ok": True,
                            "run_context": run_context})
        return behavior["result"](mission)


def _settings():
    return load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _settings_with_codex_provider(provider: str):
    """I1 是正: `settings.runner.codex.provider` を上書きした settings
    (`--provider` を省略したときに verify_backend が何を実効値として使う
    かを確かめるための土台)。"""
    s = _settings()
    return s.model_copy(update={"runner": s.runner.model_copy(
        update={"codex": s.runner.codex.model_copy(
            update={"provider": provider})})})


def _echo_ok(mission: Mission) -> MissionResult:
    nonce = mission.output_schema["properties"]["echo"]["const"]
    return MissionResult(status="completed", output={"echo": nonce, "count": 0},
                         transcript=[])


def test_verify_backend_default_requires_admission_and_fails_closed_without_host(
        tmp_path, monkeypatch):
    """既定の verify は mission と同じ admission を通り、host が無ければ拒否する。"""
    from agentic_fx.core import improve_admission

    settings = _settings()
    settings = settings.model_copy(update={"runner": settings.runner.model_copy(update={
        "improve": settings.runner.improve.model_copy(update={"backend": "codex"})})})
    monkeypatch.setattr(improve_admission, "current_host", lambda: None)

    result = _verify_backend(tmp_path, settings, backend="codex", provider=None,
                             clock=FixedClock(NOW))

    assert result.ok is False
    assert result.fingerprint is None
    assert result.detail == ("gate=sandbox_unavailable status=failed reason=- "
                             "refusal=cgroup_topology_unsupported")
    assert result.operator_detail == "sandbox_unavailable: cgroup_topology_unsupported"


@pytest.mark.parametrize(("reason", "expected"), [
    ("worker eof after private output", "worker_eof"),
    ("usage limit reached; retry later", "usage_limit"),
    ("child ready ok=false: private error", "child_ready_not_ok"),
    ("arbitrary private diagnostic", "-"),
])
def test_selftest_failure_detail_maps_only_known_reason_text(reason, expected):
    assert selftest_failure_detail("incomplete", status="failed", reason=reason) == (
        f"gate=incomplete status=failed reason={expected}")


def test_selftest_detail_sanitizer_degrades_unknown_fields_without_raising():
    class BadWatch:
        def summary(self):
            return object()

    assert selftest_failure_detail("not-a-gate", status="not-a-status", reason="private",
                                   watch=BadWatch(), unreaped="one") == (
        "gate=unknown status=- reason=-")
    assert selftest_detail_or_default("SANDBOX_UNAVAILABLE: private refusal") == (
        "gate=unknown status=failed reason=-")


@pytest.mark.parametrize("options", [{}, {"cleanup_protocol": object()}])
def test_verify_backend_rejects_incomplete_isolation_options_before_runner(
        tmp_path, monkeypatch, options):
    """admission 外から渡した不完全な options で隔離を外せない。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)

    with pytest.raises(ValueError, match="cleanup_protocol.*isolation"):
        _verify_backend(tmp_path, _settings(), backend="local", provider=None,
                        clock=FixedClock(NOW), isolation_options=options)


def test_verify_backend_sentinel_constructs_runner_without_isolation_options(
        tmp_path, monkeypatch):
    """運用者 shell の sentinel だけが隔離用の runner 引数を省略する。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}

    result = _verify_backend(
        tmp_path, _settings(), backend="local", provider=None, clock=FixedClock(NOW),
        isolation_options=OPERATOR_SHELL_NO_ISOLATION)

    assert result.ok is True
    assert "cleanup_protocol" not in _FakeVerifyWorkerRunner.captured_kwargs
    assert "isolation" not in _FakeVerifyWorkerRunner.captured_kwargs


def test_verify_backend_accepts_complete_selftest_isolation_options(tmp_path, monkeypatch):
    """自己試験が渡す完全な WorkerRunner options は隔離付きで通る。"""
    from types import SimpleNamespace

    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F

    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}
    decision = IA.AdmissionDecision(
        True, None, "local", SimpleNamespace(aggregate="b" * 64),
        F.ArtifactIdentity("a" * 64, 1, 2, 3, 4))
    options = IA.worker_runner_options(decision, object())

    result = _verify_backend(tmp_path, _settings(), backend="local", provider=None,
                             clock=FixedClock(NOW), isolation_options=options)

    assert result.ok is True
    assert _FakeVerifyWorkerRunner.captured_kwargs["cleanup_protocol"] is options["cleanup_protocol"]
    assert _FakeVerifyWorkerRunner.captured_kwargs["isolation"] is options["isolation"]


def test_verify_backend_succeeds_on_nonce_roundtrip(tmp_path, monkeypatch):
    """§8.1-45: `improve.llama_swap_verified=false` のままでも
    `verify-backend` は実行できる (これが唯一の経路)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    settings = _settings()
    assert settings.improve.llama_swap_verified is False
    _BEHAVIOR["current"] = {"result": _echo_ok}

    result = verify_backend(tmp_path, settings, backend="local",
                           provider=None, clock=FixedClock(NOW))

    assert isinstance(result, VerifyBackendResult)
    assert result.ok is True
    assert result.backend == "local"
    assert result.fingerprint is not None
    assert len(result.fingerprint) == 64  # sha256 hex


def test_verify_backend_constructs_worker_runner_with_run_context_and_rejecting_rpc(
        tmp_path, monkeypatch):
    """report-task13.md B-1/B-2/B-3 是正の核: `WorkerRunner` (本番経路) を
    `worker_profile="improve"` で構築し、`rpc_handlers` は
    run_backtest/analyze_corr を無条件に拒否する。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}

    verify_backend(tmp_path, _settings(), backend="local", provider=None,
                   clock=FixedClock(NOW))

    kwargs = _FakeVerifyWorkerRunner.captured_kwargs
    assert kwargs["worker_profile"] == "improve"
    ctx = kwargs["run_context"]
    assert kwargs["rpc_handlers"] is not ctx.rpc_handlers
    assert set(kwargs["rpc_handlers"]) == {"run_backtest", "analyze_corr"}
    expected_timeouts = {
        "run_backtest": _settings().improve.backtest_rpc_timeout_sec,
        "analyze_corr": _settings().improve.backtest_rpc_timeout_sec,
    }
    assert kwargs["rpc_timeout_sec_by_kind"] == expected_timeouts
    assert ctx.ledger._rpc_timeout_sec_by_kind == expected_timeouts
    with pytest.raises(RuntimeError):
        ctx.rpc_handlers["run_backtest"]({})
    with pytest.raises(RuntimeError):
        ctx.rpc_handlers["analyze_corr"]({})
    # F-V6 是正 (段0 F 診断4): 検証入口は「実在しない mission id」を使う
    # 規約 (実 improve の mission id (>=1、autoincrement) と名前空間が
    # 衝突しないため -1 固定)。`mission_id = -1` → `1` の変異はこれまで
    # 未検出だった (`mission_id` の値そのものを見る assert が無かった)。
    assert ctx.mission_id == -1


def test_verify_backend_does_not_touch_wave_or_backlog_or_run_tables(
        tmp_path, monkeypatch):
    """scheduler・wave・backlog・improvement_runs に一切触れない (DB 書込
    ゼロ) — §8.1-45 の「one-shot protocol」の核。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}
    (tmp_path / "data").mkdir()
    from agentic_fx.store.db import connect, init_db
    conn = connect(tmp_path / "data" / "agentic.db")
    init_db(conn)
    tables = ("improvement_backlog", "improve_waves", "improvement_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in tables}

    verify_backend(tmp_path, _settings(), backend="local", provider=None,
                   clock=FixedClock(NOW))

    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables}
    assert before == after


def test_verify_backend_fails_closed_on_nonce_mismatch(tmp_path, monkeypatch):
    """親ゲート (b): output は返るが nonce が一致しない (schema 通過の
    見せかけ) → `ok=False`、fingerprint は None。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {
        "result": lambda m: MissionResult(
            status="completed", output={"echo": "not-the-nonce"},
            transcript=[])}

    result = verify_backend(tmp_path, _settings(), backend="local",
                           provider=None, clock=FixedClock(NOW))

    assert result.ok is False
    assert result.fingerprint is None


@pytest.mark.parametrize("bad_field", [
    "mission_id", "staging_dir", "source_snapshot_dir_basename",
    "source_snapshot_dir_missing"])
def test_verify_backend_fails_closed_on_ready_run_context_mismatch(
        tmp_path, monkeypatch, bad_field):
    """A17 是正 (束D検収, verified-local-round1.md §11 #6): 親ゲート (a)
    (`on_ready` の run_context 照合 3 条件) は、`_FakeVerifyWorkerRunner`
    が常に正しい frame を返していたため、どの否定側 (mismatch) も
    スイート内で踏まれていなかった (`grep -rn "run context mismatch"
    tests/` = 0 件)。3 値 parametrize で mismatch を注入し、
    `verify_backend` 内の `on_ready` が RuntimeError を送出することを
    要求する (verify_backend はこの例外を `ok=False` へ変換せず、呼び
    出し元へそのまま伝播させる — フェイルクローズの一形態)。

    束F検収 L-F1/L-F14 是正 (裁定 C 案) により、送出型は汎用
    `RuntimeError` から専用 `VerifyBackendGateError` (`RuntimeError` の
    サブクラスではない) へ変わった — `backtest/cli.py::dispatch` の統一
    エラー境界がこの型だけを個別に catch して `エラー: ...` + rc=1 に
    畳むため (他の `RuntimeError` を握り潰す範囲は広げない)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok, "bad_ready": bad_field}

    with pytest.raises(VerifyBackendGateError, match="run_context mismatch"):
        verify_backend(tmp_path, _settings(), backend="local",
                       provider=None, clock=FixedClock(NOW))


@pytest.mark.parametrize("status", ["failed", "timeout", "max_turns"])
def test_verify_backend_fails_closed_when_status_is_not_completed_but_output_is_valid(
        tmp_path, monkeypatch, status):
    """F-V1 是正 (段0 致命1、最重要): 親ゲート — `result.status` と
    `result.output` の整合は `verify_backend` のこの 1 行だけが強制する
    (`WorkerRunner:421` は子が送ってきた status/output をそのまま透過する
    構造であり、`status != "completed"` かつ `output` が非空という組を
    拒否しているのはここだけ)。`if result.status != "completed" or not
    result.output:` を `if not result.output:` へ弱める変異 (段0 F-V1) は
    status を一切見なくなるため、`status="failed"`/`"timeout"`/
    `"max_turns"` でも nonce が一致する `output` さえ返れば `ok=True` と
    有効な fingerprint を発行してしまう。3 値 parametrize で
    `!= "completed"` を `== "failed"` へ狭める変異 (メモリ §6.7 と同型)
    まで一緒に取る。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)

    def _not_completed_but_echoing(mission: Mission) -> MissionResult:
        nonce = mission.output_schema["properties"]["echo"]["const"]
        return MissionResult(status=status, output={"echo": nonce, "count": 0},
                             transcript=[], reason="cli " + status)
    _BEHAVIOR["current"] = {"result": _not_completed_but_echoing}

    result = verify_backend(tmp_path, _settings(), backend="local",
                           provider=None, clock=FixedClock(NOW))

    # 遷移を見る形: 同じ output (nonce 一致) でも status が "completed" で
    # なければ ok=True になってはならない (test_verify_backend_succeeds_on_
    # nonce_roundtrip の completed 側と対にして踏む)。
    assert result.ok is False
    assert result.fingerprint is None
    assert f"status={status}" in result.detail


def test_verify_backend_mission_requires_the_staging_list_tool(
        tmp_path, monkeypatch):
    """F-V4/F-V5/F-V8 是正 (段0「観測点不在」): `_FakeVerifyWorkerRunner`
    は `mission` を nonce 読み出し以外に一切使わないため、
    `max_turns`/`tools`/`output_schema["additionalProperties"]` を変えても
    どの振る舞い assert にも到達しない (モックがこれらの次元を丸ごと
    捨てている — メモリ §6.12)。Mission 本体を捕獲し、組で完全一致を取る
    (メモリ §6.10)。"""
    captured: dict[str, Mission] = {}

    class _Capture(_FakeVerifyWorkerRunner):
        def run(self, mission: Mission) -> MissionResult:
            captured["m"] = mission
            return super().run(mission)

    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner", _Capture)
    _BEHAVIOR["current"] = {"result": _echo_ok}

    verify_backend(tmp_path, _settings(), backend="local", provider=None,
                   clock=FixedClock(NOW))

    m = captured["m"]
    assert (m.max_turns, m.tools, m.output_schema["additionalProperties"],
            m.output_schema["required"]) == (4, ["list_staging"], False, ["echo", "count"])


def test_verify_backend_fails_closed_when_handshake_never_reaches_ready(
        tmp_path, monkeypatch):
    """F-V7 是正 (段0「観測点不在」): 親ゲート (a) の**存在**判定
    (`if not ready_frames:`) は、`_FakeVerifyWorkerRunner` が
    (`skip_ready` を指定しない限り) 常に正しい ready frame を送るため、
    どの既存テストからも否定側 (ready を一度も受けない) が踏まれていな
    かった (`grep -rn "skip_ready" tests/` = 定義 1 件のみ、消費側は本
    テストが初)。`if not ready_frames:` → `if False:` の変異はこの分岐
    そのものを消すため、ready を一度も受けなくても後続 (nonce 一致) だけ
    で `ok=True` になってしまう。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok, "skip_ready": True}

    result = verify_backend(tmp_path, _settings(), backend="local",
                           provider=None, clock=FixedClock(NOW))

    assert result.ok is False
    assert result.fingerprint is None
    assert result.detail == "gate=handshake status=completed reason=-"


def test_verify_backend_failure_detail_discards_untrusted_reason_text(tmp_path, monkeypatch):
    """Selftest detail is bounded while the operator diagnostic retains the cause."""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    unsafe = ("worker_eof stderr=token-ABC alice@example.com "
              "https://private.invalid/v1 sk-live-credential")
    _BEHAVIOR["current"] = {
        "result": lambda _mission: MissionResult("failed", None, [], reason=unsafe)}

    result = verify_backend(tmp_path, _settings(), backend="local",
                            provider=None, clock=FixedClock(NOW))

    assert result.detail == "gate=incomplete status=failed reason=worker_eof"
    assert all(value not in result.detail for value in (
        "stderr", "alice@example.com", "https://", "sk-live-credential"))
    assert result.operator_detail == (
        f"mission did not complete: status=failed reason={unsafe}")


def test_verify_backend_fingerprint_changes_across_runs_with_identical_config(
        tmp_path, monkeypatch):
    """F-V3 是正 (段0 診断4): fingerprint 原像から `:{nonce}` を落とす変異
    は既存 pin (`len(result.fingerprint) == 64` のみ) では検出できない
    (メモリ §6.8「部分一致 assert」)。同一 backend/provider/model の
    2 回の検証で fingerprint が異なることを見て、「fingerprint は構成では
    なくこの実行の往復を表す」契約そのものを固定する (nonce を落とす変異
    の下では、他の全条件が同一なら 2 回とも同じ fingerprint になる)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}
    settings = _settings()

    result1 = verify_backend(tmp_path, settings, backend="local",
                             provider=None, clock=FixedClock(NOW))
    result2 = verify_backend(tmp_path, settings, backend="local",
                             provider=None, clock=FixedClock(NOW))

    assert result1.ok is True and result2.ok is True
    assert result1.fingerprint != result2.fingerprint


def test_verify_backend_fails_closed_when_mission_never_completes(
        tmp_path, monkeypatch):
    """親ゲート不合格 (mission failed) なら `ok=False`、fingerprint は None。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {
        "result": lambda m: MissionResult(
            status="failed", output=None, transcript=[],
            reason="mission failed")}

    result = verify_backend(tmp_path, _settings(), backend="codex",
                           provider="chatgpt", clock=FixedClock(NOW))

    assert result.ok is False
    assert result.fingerprint is None


def test_verify_backend_overrides_runner_improve_backend_and_codex_provider(
        tmp_path, monkeypatch):
    """report-task13.md B-4: `--backend`/`--provider` は実行時にだけ
    `runner.improve.backend`/`runner.codex.provider` を上書きし、個人設定
    ファイル (`settings.yaml`) は変更しない。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}
    settings = _settings()
    assert settings.runner.improve.backend == "local"  # settings.yaml.example の既定

    verify_backend(tmp_path, settings, backend="codex", provider="llama_swap",
                   clock=FixedClock(NOW))

    scoped = _FakeVerifyWorkerRunner.captured_kwargs["settings"]
    assert scoped.runner.improve.backend == "codex"
    assert scoped.runner.codex.provider == "llama_swap"
    assert settings.runner.improve.backend == "local"  # 元の settings は無変更
    # C1 是正 (束D検収, verified-local-round1.md §11 #19): 元は
    # `or True` が付いており恒真 assert だった (settings.yaml.example の
    # 既定 codex.provider は "llama_swap" ではないため、`or True` を
    # 落とすと有意な assert になる)。
    assert settings.runner.codex.provider != "llama_swap"  # 元の複製元は無変更


def test_verify_backend_opencode_bypasses_llama_swap_verified_gate_with_warning_log(
        tmp_path, monkeypatch, caplog):
    """通常入口 (`ImproveSupervisor`) の拒否契約は変更しない — verify-backend
    だけが `improve.llama_swap_verified=false` のままバイパスでき、その旨を
    WARNING ログへ残す (report-task13.md B-4)。実際の拒否経路の検証は
    Task 9 のテストが担う。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    # report-task13.md B-4: ゲート (c)/(d) の検証は backend != "local" 時に
    # 実 CLI 子プロセス起動を黒箱観測するもの。fake runner では subprocess
    # が起動しないため、watcher をモック。本来は verify_backend_realbackend.py
    # で実プロセス経由で検証される。
    class _FakeWatcher:
        def __init__(self, pid: int) -> None:
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            pass
        def saw_any_descendant(self) -> bool:
            return True  # Mock: pretend subprocess was spawned
    monkeypatch.setattr("agentic_fx.loops.verify_backend._DescendantWatcher",
                        _FakeWatcher)
    _BEHAVIOR["current"] = {"result": _echo_ok}
    settings = _settings()
    assert settings.improve.llama_swap_verified is False

    with caplog.at_level("WARNING", logger="agentic_fx.loops.verify_backend"):
        result = verify_backend(tmp_path, settings, backend="opencode",
                               provider=None, clock=FixedClock(NOW))

    assert result.ok is True
    assert any("llama_swap_verified" in r.message for r in caplog.records)


def test_verify_backend_opencode_has_no_codex_provider(
        tmp_path, monkeypatch, caplog):
    """opencode 検証に codex provider を漏らす変異を防ぐ。fingerprint は
    backend と model/nonce に基づき、provider は常に None である。"""
    class _Capture(_FakeVerifyWorkerRunner):
        def run(self, mission):
            captured_mission["m"] = mission
            return super().run(mission)
    captured_mission: dict = {}
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner", _Capture)
    monkeypatch.setattr("agentic_fx.loops.verify_backend._DescendantWatcher",
                        lambda pid: _FixedWatcher(saw=True))
    monkeypatch.setattr("agentic_fx.loops.verify_backend._descendant_pids",
                        lambda pid: set())
    _BEHAVIOR["current"] = {"result": _echo_ok}
    settings = _settings()
    assert settings.improve.llama_swap_verified is False

    with caplog.at_level(logging.WARNING, logger="agentic_fx.loops.verify_backend"):
        result = verify_backend(tmp_path, settings, backend="opencode",
                               provider=None, clock=FixedClock(NOW))

    eff_model = _FakeVerifyWorkerRunner.captured_kwargs["settings"].runner.improve.model
    nonce = captured_mission["m"].output_schema["properties"]["echo"]["const"]
    assert result.provider is None
    assert "provider=None" in result.detail
    assert any("llama_swap_verified" in r.message for r in caplog.records)
    assert result.fingerprint == hashlib.sha256(
        f"opencode:None:{eff_model}:{nonce}".encode("utf-8")).hexdigest()


@pytest.mark.parametrize("backend", ["local", "claude"])
def test_verify_backend_does_not_leak_codex_provider_into_non_codex_backends(
        tmp_path, monkeypatch, backend):
    """I1 是正の負の脚: `backend="local"`/`"claude"` では
    `settings.runner.codex.provider` が `result.provider` へ漏れない
    (effective_provider の計算を `backend == "codex"` で条件付けない回帰
    を防ぐ)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    monkeypatch.setattr("agentic_fx.loops.verify_backend._DescendantWatcher",
                        lambda pid: _FixedWatcher(saw=True))
    monkeypatch.setattr("agentic_fx.loops.verify_backend._descendant_pids",
                        lambda pid: set())
    _BEHAVIOR["current"] = {"result": _echo_ok}
    settings = _settings_with_codex_provider("llama_swap")

    result = verify_backend(tmp_path, settings, backend=backend,
                           provider=None, clock=FixedClock(NOW))

    assert result.provider is None


def test_verify_backend_watches_this_process_as_watcher_root(
        tmp_path, monkeypatch):
    """L-F2 是正 (verified-local-round1.md §1【2】): `_DescendantWatcher` は
    自プロセス (`os.getpid()`) を root にして生成される — 配線が
    `_DescendantWatcher(1)` 等の恒真値 (全プロセスを拾う PID) に壊れても
    既定スイートは検知していなかった (判定側は `_FixedWatcher` で pin
    済みだが、`_DescendantWatcher(...)` へ渡る実引数そのものは未 pin)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    seen: list[int] = []

    def _spy_watcher(pid):
        seen.append(pid)
        return _FixedWatcher(saw=True)

    monkeypatch.setattr(
        "agentic_fx.loops.verify_backend._DescendantWatcher", _spy_watcher)
    monkeypatch.setattr(
        "agentic_fx.loops.verify_backend._descendant_pids", lambda pid: set())
    _BEHAVIOR["current"] = {"result": _echo_ok}

    verify_backend(tmp_path, _settings(), backend="codex",
                   provider="chatgpt", clock=FixedClock(NOW))

    assert seen == [os.getpid()]


def test_verify_backend_scratch_dirs_have_expected_modes_and_are_removed(
        tmp_path, monkeypatch):
    """L-F7/L-F8 是正 (verified-local-round1.md §1【9】【10】): `verify_backend`
    が自分で作る scratch (`staging_dir`/`source_snapshot_dir`) の権限
    (0o700/0o500) と、`finally: shutil.rmtree(scratch_root, ...)` による
    後始末は既定スイートで一度も pin されていなかった (grep 全数確認済み
    — `0o500`/`afx-verify-backend` を検査する既存テストは 0 件)。
    `tempfile.mkdtemp()` は `/tmp` に作るため、後始末漏れは
    `afx-verify-backend-*` の恒久残留になる (検証入口は人間が繰り返し
    叩く前提のため蓄積する)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    seen: dict[str, object] = {}

    def _capture(mission: Mission) -> MissionResult:
        ctx = _FakeVerifyWorkerRunner.captured_kwargs["run_context"]
        seen["staging_mode"] = ctx.staging_dir.stat().st_mode & 0o777
        seen["source_mode"] = ctx.source_snapshot_dir.stat().st_mode & 0o777
        seen["scratch_root"] = ctx.staging_dir.parent.parent
        return _echo_ok(mission)

    _BEHAVIOR["current"] = {"result": _capture}

    verify_backend(tmp_path, _settings(), backend="local", provider=None,
                   clock=FixedClock(NOW))

    assert seen["staging_mode"] == 0o700
    assert seen["source_mode"] == 0o500
    assert not seen["scratch_root"].exists(), (
        "verify_backend の finally が scratch_root を削除していない "
        "(afx-verify-backend-* の /tmp 残留)")


class _FixedWatcher:
    """F-2 是正 (プラン10 Task13 検収): 親ゲート (c)/(d) を `ok=False` 側から
    踏むための固定応答 watcher。`saw_any_descendant()` の戻り値を注入する
    (既存の `test_..._bypasses_llama_swap_verified_gate_with_warning_log`
    と同じ発想の monkeypatch — `_DescendantWatcher` はモジュール直下の
    グローバル参照なので、生成時ではなく呼び出し時に解決される。よって
    monkeypatch だけで注入可能であり、`verify_backend.py` 本体の改修は
    不要)。"""

    def __init__(self, saw: bool) -> None:
        self._saw = saw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def saw_any_descendant(self) -> bool:
        return self._saw


def test_verify_backend_fails_closed_when_no_cli_descendant_is_observed(
        tmp_path, monkeypatch):
    """F-2 是正: 親ゲート (c) — `backend != "local"` なのに `_DescendantWatcher`
    が子孫プロセスを 1 件も観測しなかった場合、`ok=False` かつ detail に
    `cli_started gate` を含む (実 CLI が起動していないのに `ok=True` を
    返してしまう欠陥の pin)。gate (d) (`after_descendants`) 側の
    `_descendant_pids` も併せて空集合へ固定し、gate (d) 側の判定に
    フォールスルーして偶然 ok=False になる形 (テストの偽陽性) を防ぐ。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    monkeypatch.setattr("agentic_fx.loops.verify_backend._DescendantWatcher",
                        lambda pid: _FixedWatcher(saw=False))
    monkeypatch.setattr("agentic_fx.loops.verify_backend._descendant_pids",
                        lambda pid: set())
    _BEHAVIOR["current"] = {"result": _echo_ok}

    result = verify_backend(tmp_path, _settings(), backend="codex",
                           provider="chatgpt", clock=FixedClock(NOW))

    assert result.ok is False
    assert result.fingerprint is None
    assert result.detail == "gate=no_cli_child status=completed reason=-"


def test_verify_backend_fails_closed_when_descendants_are_not_reaped(
        tmp_path, monkeypatch):
    """F-2 是正: 親ゲート (d) — mission 完了後も子孫プロセスが残っている
    (pgid 回収漏れ) 場合、`ok=False` かつ detail に `pgid recovery gate`
    を含む。gate (c) 側は素通りさせるため watcher は観測ありに固定する
    (gate (c) が先に落ちて (d) を通らないままテストが偶然 pass する形の
    偽陽性を防ぐ)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    monkeypatch.setattr("agentic_fx.loops.verify_backend._DescendantWatcher",
                        lambda pid: _FixedWatcher(saw=True))
    monkeypatch.setattr("agentic_fx.loops.verify_backend._descendant_pids",
                        lambda pid: {999999})
    _BEHAVIOR["current"] = {"result": _echo_ok}

    result = verify_backend(tmp_path, _settings(), backend="codex",
                           provider="chatgpt", clock=FixedClock(NOW))

    assert result.ok is False
    assert result.fingerprint is None
    assert result.detail == "gate=not_reaped status=completed reason=- unreaped=1"


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="/proc 依存 (verify_backend._descendant_pids)")
def test_descendant_pids_recurses_into_grandchildren(tmp_path):
    """A18 後半/L-B17 是正 (束D検収, verified-local-round1.md §11 #11):
    実 `_DescendantWatcher`/実 `_descendant_pids` は既定スイートで一度も
    実行されていなかった (F-2 の pin は両方とも fake/monkeypatch で
    差し替えられ、`test_verify_backend_realbackend.py` は
    `addopts = -m 'not bench and not realbackend'` で既定 deselect)。
    ここでは実子プロセス (`sh -c "sleep 5 & wait"` — 孫として `sleep` を
    起こす) を実際に起動し、`_descendant_pids` が孫まで再帰的に拾うこと
    を実測する (`realbackend` マーカーは不要 — CLI backend を一切使わない
    純粋な `/proc` 走査プローブ)。"""
    import subprocess

    from agentic_fx.loops.verify_backend import _descendant_pids

    proc = subprocess.Popen(["sh", "-c", "sleep 5 & wait"])
    try:
        deadline = time.monotonic() + 3
        descendants: set[int] = set()
        while time.monotonic() < deadline:
            descendants = _descendant_pids(os.getpid())
            if len(descendants) >= 2:
                break
            time.sleep(0.05)
        assert proc.pid in descendants, (
            f"直接の子 (sh) が見つからない: {descendants}")
        assert len(descendants) >= 2, (
            f"孫 (sleep) まで再帰的に拾えていない: {descendants}")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="/proc 依存 (verify_backend._DescendantWatcher)")
def test_descendant_watcher_observes_a_real_child_process(tmp_path):
    """A18 後半/L-B17 是正 (束D検収, verified-local-round1.md §11 #11):
    実 `_DescendantWatcher` (`__enter__`/`__exit__` の間、0.05s 間隔で
    ポーリングする daemon thread) を実子プロセスで駆動し、
    `saw_any_descendant()` が True になることを実測する。"""
    import subprocess

    from agentic_fx.loops.verify_backend import _DescendantWatcher

    with _DescendantWatcher(os.getpid()) as watcher:
        proc = subprocess.Popen(["sleep", "0.3"])
        proc.wait(timeout=5)

    assert watcher.saw_any_descendant() is True


def test_verify_backend_output_schema_every_property_has_type_key(
        tmp_path, monkeypatch):
    """実機 E2E (2026-08-30): ChatGPT backend は output_schema の全 property に
    "type" キーを要求する (無いと 400 `Invalid schema for response_format` —
    llama-swap は非厳格で素通りするため fake/local では見えない)。"""
    captured = {}

    class _CapturingRunner(_FakeVerifyWorkerRunner):
        def run(self, mission):
            captured["schema"] = mission.output_schema
            return super().run(mission)

    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _CapturingRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok}

    verify_backend(tmp_path, _settings(), backend="local",
                   provider=None, clock=FixedClock(NOW))

    for name, prop in captured["schema"]["properties"].items():
        assert "type" in prop, f"property {name!r} lacks 'type'"


# --- 隔離の互換性自己試験 (stop_on_echo) -----------------------------------------------------

import json  # noqa: E402
import threading  # noqa: E402


def _event(content: dict) -> dict:
    """worker が親へ送る event frame の実形 (CliRunner の包みを worker がもう一度包む。実機の
    観測)。"""
    return {"type": "event", "message": {"type": "event", "message": {
        "role": "system", "content": json.dumps(content)}}}


def _mcp(kind="tools/list", tools=("web_search", "fetch_article")) -> dict:
    """worker が自分で送る MCP 観測の event (包みは 1 重)。"""
    return {"type": "event", "message": {"role": "system", "content": json.dumps(
        {"_mcp_observed": {"kind": kind, "tools": list(tools)}})}}


def _mcp_call() -> dict:
    return _mcp("tools/call", ("list_staging",))


def _claude_text(text: str) -> dict:
    return _event({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


class _StreamingRunner(_FakeVerifyWorkerRunner):
    """CLI の stdout の event を `on_event` へ流す替え玉。`stop_event` が立つまで次の
    event を流し続け (実 CLI が止まらない様子)、上限の step に達したら終わる。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_event = kwargs.get("on_event")
        self._stop = kwargs.get("stop_event")

    def run(self, mission: Mission) -> MissionResult:
        nonce = mission.output_schema["properties"]["echo"]["const"]
        behavior = _BEHAVIOR["current"]
        behavior.setdefault("result", lambda m: None)
        super().run(mission)  # ready
        events = behavior["events"](nonce)
        delivered = 0
        for ev in events:
            if self._stop is not None and self._stop.is_set():
                break
            if self._on_event is not None:
                self._on_event(ev)
            delivered += 1
        behavior["delivered"] = delivered
        if self._stop is not None and self._stop.is_set():
            return MissionResult(status="timeout", output=None, transcript=[])
        return behavior["final"]


def _watchers(monkeypatch):
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner", _StreamingRunner)
    monkeypatch.setattr("agentic_fx.loops.verify_backend._DescendantWatcher",
                        lambda pid: _FixedWatcher(saw=True))
    monkeypatch.setattr("agentic_fx.loops.verify_backend._descendant_pids", lambda pid: set())


def _run_stop_on_echo(tmp_path, backend="opencode"):
    return verify_backend(tmp_path, _settings(), backend=backend, provider=None,
                          clock=FixedClock(NOW), stop_on_echo=True)


def test_stop_on_echo_succeeds_at_first_echo_even_if_cli_keeps_calling_tools(
        tmp_path, monkeypatch):
    """最初の応答で echo が一致したら、その後に CLI が tool を呼び続けても成功は覆らない。
    echo の後の event は流れてこない (selftest が停止経路で mission を終える)。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"type": "step_start"})
        yield _mcp("initialize", ())
        yield _mcp()
        yield _mcp_call()
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})
        yield _event({"type": "step_finish", "part": {"reason": "tool-calls"}})
        for _ in range(1000):
            yield _event({"type": "tool_use", "part": {"tool": "afx_web_search"}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)

    assert result.ok is True and result.fingerprint is not None
    assert _BEHAVIOR["current"]["delivered"] == 5


def test_stop_on_echo_does_not_count_the_prompt_or_wrong_nonce(tmp_path, monkeypatch):
    """prompt の中に nonce が文章として現れる event や、別の nonce の echo は一致と見ない。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"type": "user", "text": f'Reply with exactly {{"echo": "{nonce}"}}'})
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": "other"})}})
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "x": 1})}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)

    assert result.ok is False
    assert "echo_reached=no" in result.detail


def test_stop_on_echo_waits_for_mcp_when_echo_comes_first(tmp_path, monkeypatch):
    """echo が先に出ても、MCP の tools/list (1 件以上) が揃うまで止めない。揃った event で止まる。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})
        yield _mcp("initialize", ())
        yield _mcp()
        yield _mcp_call()
        for _ in range(100):
            yield _event({"type": "tool_use", "part": {"tool": "afx_web_search"}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    assert _run_stop_on_echo(tmp_path).ok is True
    assert _BEHAVIOR["current"]["delivered"] == 4


def test_stop_on_echo_fails_without_tools_list(tmp_path, monkeypatch):
    """tools/list を返さない worker (MCP 未接続) は、echo が出て CLI が正常終了しても失敗。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    ok_output = MissionResult("completed", {"echo": "x"}, [])
    _BEHAVIOR["current"] = {"events": events, "final": ok_output}
    result = _run_stop_on_echo(tmp_path)
    assert result.ok is False
    assert "watch=echo_reached=yes,mcp=none,call=0,steps=0,events=1" in result.detail


def test_stop_on_echo_fails_when_tools_list_is_empty(tmp_path, monkeypatch):
    _watchers(monkeypatch)

    def events(nonce):
        yield _mcp("initialize", ())
        yield _mcp(tools=())
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)
    assert result.ok is False and "mcp=tools=0,(empty),call=0" in result.detail


def test_stop_on_echo_ignores_a_cli_printed_mcp_observation(tmp_path, monkeypatch):
    """CLI の stdout に偽の観測を出されても (包みが 2 重) MCP の観測にならない。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"_mcp_observed": {"kind": "tools/list", "tools": ["afx_x"]}})
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)
    assert result.ok is False and "mcp=none" in result.detail


@pytest.mark.parametrize("backend,event", [
    ("opencode", lambda n: {"type": "tool_result", "part": {"text": n}}),
    ("opencode", lambda n: {"_resume_recovery": True, "stderr_tail": json.dumps({"echo": n, "count": 0})}),
    ("opencode", lambda n: {"type": "reasoning", "part": {"text": json.dumps({"echo": n, "count": 0})}}),
    ("claude", lambda n: {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": json.dumps({"echo": n, "count": 0})}]}}),
    ("claude", lambda n: {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": json.dumps({"echo": n, "count": 0})}]}}),
    ("codex", lambda n: {"type": "item.completed", "item": {
        "type": "command_execution", "aggregated_output": json.dumps({"echo": n, "count": 0})}}),
    ("codex", lambda n: {"type": "item.completed", "item": {
        "type": "reasoning", "text": json.dumps({"echo": n, "count": 0})}}),
])
def test_stop_on_echo_ignores_nonce_json_outside_assistant_text(
        tmp_path, monkeypatch, backend, event):
    _watchers(monkeypatch)

    def events(nonce):
        yield _mcp("initialize", ())
        yield _mcp()
        yield _event(event(nonce))

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path, backend=backend)
    assert result.ok is False and "echo_reached=no" in result.detail


def test_stop_on_echo_accepts_each_backends_assistant_text(tmp_path, monkeypatch):
    _watchers(monkeypatch)
    shapes = {
        "opencode": lambda n: {"type": "text", "part": {"text": json.dumps({"echo": n, "count": 0})}},
        "claude": lambda n: {"type": "result", "result": json.dumps({"echo": n, "count": 0})},
        "codex": lambda n: {"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps({"echo": n, "count": 0})}},
    }
    for backend, shape in shapes.items():
        def events(nonce, shape=shape):
            yield _mcp("initialize", ())
            yield _mcp()
            yield _mcp_call()
            yield _event(shape(nonce))

        _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
        assert _run_stop_on_echo(tmp_path, backend=backend).ok is True, backend


@pytest.mark.parametrize("wrap", [
    lambda t: "```\n" + t + "\n```trailing",
    lambda t: "intro\n```json\n" + t + "\n```",
    lambda t: "```json\n" + t + "\n```\n```json\n" + t + "\n```",
    lambda t: "`" + t + "`",
])
def test_stop_on_echo_requires_one_complete_code_fence(tmp_path, monkeypatch, wrap):
    _watchers(monkeypatch)

    def events(nonce):
        yield _mcp("initialize", ())
        yield _mcp()
        yield _event({"type": "text", "part": {"text": wrap(json.dumps({"echo": nonce, "count": 0}))}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    assert _run_stop_on_echo(tmp_path).ok is False


def test_stop_on_echo_accepts_fenced_json_and_nested_claude_shape(tmp_path, monkeypatch):
    _watchers(monkeypatch)

    def events(nonce):
        yield _mcp("initialize", ())
        yield _mcp()
        yield _mcp_call()
        yield _event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "```json\n" + json.dumps({"echo": nonce, "count": 0}) + "\n```"}]}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    assert _run_stop_on_echo(tmp_path, backend="claude").ok is True


def test_without_stop_on_echo_the_normal_result_decides(tmp_path, monkeypatch):
    """既定 (stop_on_echo なし) の verify-backend は従来どおり mission の結果で判定する。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    _BEHAVIOR["current"] = {"events": events,
                            "final": MissionResult("failed", None, [], reason="boom")}
    result = verify_backend(tmp_path, _settings(), backend="opencode", provider=None,
                            clock=FixedClock(NOW))
    assert result.ok is False and result.detail == "gate=incomplete status=failed reason=-"
    assert "boom" in result.operator_detail
    assert _StreamingRunner.captured_kwargs.get("stop_event") is None


def test_stop_on_echo_failure_keeps_cli_result_and_reports_progress(tmp_path, monkeypatch):
    """echo に届かず mission が worker の cleanup_deadline_received で終わっても、reason を
    上書きせず、echo 未到達と経過 step 数・event 数を detail に残す。"""
    _watchers(monkeypatch)

    def events(nonce):
        for _ in range(3):
            yield _event({"type": "step_start"})
            yield _event({"type": "tool_use", "part": {"tool": "afx_fetch_article"}})
            yield _event({"type": "step_finish", "part": {"reason": "tool-calls"}})

    _BEHAVIOR["current"] = {
        "events": events,
        "final": MissionResult("failed", None, [], reason="cleanup_deadline_received")}
    result = _run_stop_on_echo(tmp_path)

    assert result.ok is False
    assert "reason=-" in result.detail
    assert "cleanup_deadline_received" in result.operator_detail
    assert "watch=echo_reached=no,mcp=none,call=0,steps=3,events=9" in result.detail
    assert "steps=3" in result.detail and "events=9" in result.detail


def test_stop_on_echo_timeout_without_echo_is_readable(tmp_path, monkeypatch):
    _watchers(monkeypatch)

    def events(nonce):
        yield _event({"type": "step_finish", "part": {"reason": "tool-calls"}})
        yield _event({"type": "step_finish", "part": {"reason": "tool-calls"}})

    _BEHAVIOR["current"] = {"events": events,
                            "final": MissionResult("timeout", None, [])}
    result = _run_stop_on_echo(tmp_path)

    assert result.ok is False
    assert "status=timeout" in result.detail
    assert "watch=echo_reached=no,mcp=none,call=0,steps=2,events=2" in result.detail


def test_stop_on_echo_still_requires_cli_child_and_reaping(tmp_path, monkeypatch):
    """echo が出ても、CLI の子孫が観測されない / 回収されないなら成功にしない。"""
    _watchers(monkeypatch)
    monkeypatch.setattr("agentic_fx.loops.verify_backend._descendant_pids",
                        lambda pid: {999999})

    def events(nonce):
        yield _mcp("initialize", ())
        yield _mcp()
        yield _mcp_call()
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)
    assert result.ok is False and result.detail == "gate=not_reaped status=timeout reason=- unreaped=1"


def test_stop_on_echo_fails_when_tools_list_comes_without_initialize(tmp_path, monkeypatch):
    """initialize の観測が無いまま tools/list だけ届いても MCP 接続とは見ない。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _mcp()
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)
    assert result.ok is False and "mcp=tools=2,(uninitialized),call=0" in result.detail


def test_stop_on_echo_summary_marks_uninitialized_only_when_initialize_was_not_seen(
        tmp_path, monkeypatch):
    """initialize と tools/list は届いて tools/call だけ無いときは (uninitialized) を付けない。"""
    _watchers(monkeypatch)

    def events(nonce):
        yield _mcp("initialize", ())
        yield _mcp()
        yield _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})

    _BEHAVIOR["current"] = {"events": events, "final": MissionResult("failed", None, [])}
    result = _run_stop_on_echo(tmp_path)
    assert result.ok is False
    assert "mcp=tools=2,call=0" in result.detail and "uninitialized" not in result.detail
    assert "call=0" in result.detail


class _FakeMono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _echo_frame(nonce: str) -> dict:
    return _event({"type": "text", "part": {"text": json.dumps({"echo": nonce, "count": 0})}})


def test_echo_watch_grace_expires_when_tools_list_never_comes_after_echo():
    from agentic_fx.loops.verify_backend import _EchoWatch

    clock, stop = _FakeMono(), threading.Event()
    watch = _EchoWatch("n1", stop, "opencode", mcp_grace_sec=60.0, monotonic=clock)
    clock.t = 5.0
    assert watch.check_grace() is False  # echo 未到達の間は猶予を数えない
    clock.t = 1000.0
    assert watch.check_grace() is False and not stop.is_set()
    watch.observe(_echo_frame("n1"))
    clock.t = 1059.0
    assert watch.check_grace() is False and not stop.is_set()
    clock.t = 1060.0
    assert watch.check_grace() is True and stop.is_set()
    assert not watch.ok
    assert "mcp=none" in watch.summary() and "grace_expired" in watch.summary()


def test_echo_watch_tools_list_within_grace_succeeds():
    from agentic_fx.loops.verify_backend import _EchoWatch

    clock, stop = _FakeMono(), threading.Event()
    watch = _EchoWatch("n1", stop, "opencode", mcp_grace_sec=60.0, monotonic=clock)
    watch.observe(_echo_frame("n1"))
    watch.observe(_mcp("initialize", ()))
    clock.t = 59.0
    watch.observe(_mcp())
    watch.observe(_mcp_call())
    assert watch.ok and stop.is_set()
    clock.t = 500.0
    assert watch.check_grace() is False and "grace_expired" not in watch.summary()


def test_echo_watch_deadline_wins_when_tools_list_arrives_at_the_deadline():
    from agentic_fx.loops.verify_backend import _EchoWatch

    for grace_first in (True, False):
        clock, stop = _FakeMono(), threading.Event()
        watch = _EchoWatch("n1", stop, "opencode", mcp_grace_sec=60.0, monotonic=clock)
        watch.observe(_echo_frame("n1"))
        watch.observe(_mcp("initialize", ()))
        clock.t = 60.0
        if grace_first:
            assert watch.check_grace() is True
            watch.observe(_mcp())
        else:
            watch.observe(_mcp())
            assert watch.check_grace() is True
        assert not watch.ok and stop.is_set()
        assert "grace_expired" in watch.summary()


def test_echo_watch_success_summary_has_no_grace_expired_after_ok():
    from agentic_fx.loops.verify_backend import _EchoWatch

    clock, stop = _FakeMono(), threading.Event()
    watch = _EchoWatch("n1", stop, "opencode", mcp_grace_sec=60.0, monotonic=clock)
    watch.observe(_echo_frame("n1"))
    watch.observe(_mcp("initialize", ()))
    clock.t = 59.9
    watch.observe(_mcp())
    watch.observe(_mcp_call())
    clock.t = 60.0
    assert watch.check_grace() is False
    assert watch.ok and "grace_expired" not in watch.summary()

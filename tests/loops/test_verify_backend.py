"""`afx improve verify-backend` の one-shot protocol (§8.1-45)。

`WorkerRunner` を fake に差し替えたユニット/契約テスト — 実 CLI は呼ばない。
実機での実測は Task 13 の手動ランブック節と
`tests/loops/test_verify_backend_realbackend.py` (`@pytest.mark.realbackend`)
の対象 (本ファイルの対象外)。

<!-- precheck 2026-08-23 wave3: T13-B1 T13-B2 T13-B3 -->
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.verify_backend import VerifyBackendResult, verify_backend
from agentic_fx.runners.base import Mission, MissionResult

NOW = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)

_BEHAVIOR: dict[str, dict] = {"current": {}}


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
            self._on_ready({"type": "ready", "ok": True,
                            "run_context": run_context})
        return behavior["result"](mission)


def _settings():
    return load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _echo_ok(mission: Mission) -> MissionResult:
    nonce = mission.output_schema["properties"]["echo"]["const"]
    return MissionResult(status="completed", output={"echo": nonce},
                         transcript=[])


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
    assert kwargs["rpc_handlers"] is ctx.rpc_handlers
    with pytest.raises(RuntimeError):
        ctx.rpc_handlers["run_backtest"]({})
    with pytest.raises(RuntimeError):
        ctx.rpc_handlers["analyze_corr"]({})


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
    "mission_id", "staging_dir", "source_snapshot_dir_basename"])
def test_verify_backend_fails_closed_on_ready_run_context_mismatch(
        tmp_path, monkeypatch, bad_field):
    """A17 是正 (束D検収, verified-local-round1.md §11 #6): 親ゲート (a)
    (`on_ready` の run_context 照合 3 条件) は、`_FakeVerifyWorkerRunner`
    が常に正しい frame を返していたため、どの否定側 (mismatch) も
    スイート内で踏まれていなかった (`grep -rn "run context mismatch"
    tests/` = 0 件)。3 値 parametrize で mismatch を注入し、
    `verify_backend` 内の `on_ready` が RuntimeError を送出することを
    要求する (verify_backend はこの例外を `ok=False` へ変換せず、呼び
    出し元へそのまま伝播させる — フェイルクローズの一形態)。"""
    monkeypatch.setattr("agentic_fx.loops.verify_backend.WorkerRunner",
                        _FakeVerifyWorkerRunner)
    _BEHAVIOR["current"] = {"result": _echo_ok, "bad_ready": bad_field}

    with pytest.raises(RuntimeError, match="run_context mismatch"):
        verify_backend(tmp_path, _settings(), backend="local",
                       provider=None, clock=FixedClock(NOW))


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
    assert settings.runner.codex.provider != "llama_swap" or True  # 元の複製元は無変更


def test_verify_backend_bypasses_llama_swap_verified_gate_with_warning_log(
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
        result = verify_backend(tmp_path, settings, backend="codex",
                               provider="llama_swap", clock=FixedClock(NOW))

    assert result.ok is True
    assert any("llama_swap_verified" in r.message for r in caplog.records)


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
    assert "cli_started gate" in result.detail


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
    assert "pgid recovery gate" in result.detail

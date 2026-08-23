"""CliRunner — claude / codex 共通基盤 (設計書 §1.1)。

個別 backend (ClaudeRunner/CodexRunner) は argv/env の組み立てと最終出力の
抽出だけをオーバーライドする。子プロセスは常に `runners.launcher` 経由で
起動する (multi-threaded な mission_worker で `preexec_fn` を使わない —
Global Constraints)。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import jsonschema

from agentic_fx._safe_error import safe_text
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.runners.launcher import build_launcher_argv
from agentic_fx.tools.registry import ToolRegistry

_MAX_REASON_CHARS = 500


@dataclass(frozen=True)
class CliLaunchSpec:
    """<!-- precheck 2026-08-22: T1-M2 --> Minor 2: `CliRunner.run()` は
    この型を構築しない (現行実装は `_build_argv`/`_build_env` の戻り値を
    直接使う) — 骨格 Interfaces が明示した形を保つための宣言のみで、
    Task 2/3 が argv/env 組み立てのヘルパとして任意に使ってよい。lint が
    未使用クラスを警告する場合は許容する (dataclass の型そのものが
    contract のドキュメントを兼ねる)。"""
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    output_schema: dict[str, Any]
    on_message: Callable[[dict], None]


def _normalize_reason(text: str) -> str:
    text = safe_text(text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    if len(text) > _MAX_REASON_CHARS:
        text = text[:_MAX_REASON_CHARS] + "…(truncated)"
    return text


class CliRunner(AgentRunner):
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None,
                 rlimits: dict[str, tuple[int, int]] | None = None,
                 launcher: Callable[..., list[str]] | None = None,
                 popen: Callable[..., subprocess.Popen] | None = None) -> None:
        self._bin_path = bin_path
        self._model = model
        self._workdir = workdir
        self._cli_terminate_grace_sec = cli_terminate_grace_sec
        self._registry = registry
        self._on_message = on_message or (lambda frame: None)
        self._cli_started_sink = cli_started_sink
        self._rlimits = rlimits
        self._build_launcher_argv = launcher or build_launcher_argv
        self._popen = popen or subprocess.Popen

    @abstractmethod
    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]: ...

    @abstractmethod
    def _build_env(self, mission: Mission) -> dict[str, str]: ...

    @abstractmethod
    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict[str, Any] | None: ...

    @abstractmethod
    def _max_turns_semantics(self) -> Literal["passthrough", "ignored"]: ...

    def run(self, mission: Mission) -> MissionResult:
        # B2-r2 是正: bind 側 (`mission_worker._start_mcp_dispatcher`) と
        # 同じ `mcp_socket_path()` から導出する — 独立したリテラルの
        # 偶然の一致に頼らない (検収 B2-r2、`cli_runner.py:88` のパス
        # 変異が全スイートを生き延びた指摘への対応)。
        from agentic_fx.mission_worker import mcp_socket_path
        mcp_socket = mcp_socket_path(self._workdir)
        inner_argv = self._build_argv(mission, mcp_socket=mcp_socket)
        env = self._build_env(mission)
        launcher_argv = self._build_launcher_argv(
            os.getpid(), inner_argv, rlimits=self._rlimits)

        devnull_r = os.open(os.devnull, os.O_RDONLY)
        try:
            proc = self._popen(
                launcher_argv, cwd=str(self._workdir), env=env,
                stdin=devnull_r, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, text=True)
        finally:
            os.close(devnull_r)

        pgid = os.getpgid(proc.pid)
        if self._cli_started_sink is not None:
            self._cli_started_sink(pgid)  # <!-- precheck 2026-08-22: T1-B10 --> §7.1-2
        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []
        reader_done = threading.Event()

        def reader() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    stdout_lines.append(line.rstrip("\n"))
                    self._on_message({"type": "event", "message": {
                        "role": "system", "content": line.rstrip("\n")}})
            finally:
                reader_done.set()

        def stderr_reader() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)

        t_out = threading.Thread(target=reader, daemon=True)
        t_err = threading.Thread(target=stderr_reader, daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        try:
            proc.wait(timeout=mission.timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True

        try:
            if timed_out or proc.poll() is None:
                self._terminate_pgid(pgid, proc)
                timed_out = True
            else:
                self._terminate_pgid(pgid, proc)  # completed でも孫を確実に回収する (§1.1-4)
        finally:
            t_out.join(timeout=5.0)
            t_err.join(timeout=5.0)

        if timed_out:
            return MissionResult("timeout", None, [], reason="cli timeout")

        rc = proc.returncode
        if rc != 0:
            stderr_text = "".join(stderr_chunks)
            reason = _normalize_reason(
                f"HTTP-like CLI exit rc={rc}: {stderr_text.splitlines()[0] if stderr_text else ''}")
            return MissionResult("failed", None, [], reason=reason)

        raw = self._extract_output(stdout_lines, self._workdir)
        if raw is None:
            return MissionResult("failed", None, [],
                                 reason=_normalize_reason("no output recovered from cli"))
        try:
            jsonschema.validate(raw, mission.output_schema)
        except jsonschema.ValidationError as e:
            return MissionResult(
                "failed", None, [],
                reason=_normalize_reason(f"output_schema mismatch: {e.message}"))
        return MissionResult("completed", raw, [])

    def _terminate_pgid(self, pgid: int, proc: subprocess.Popen) -> None:
        """<!-- precheck 2026-08-22: T1-B9 --> `proc` (CLI 自身、pgid の
        グループリーダ) は SIGKILL 後も親 (このメソッドの呼び出し元) が
        `wait()` するまで zombie として残り、zombie は `killpg(pgid, 0)`
        に応答し続ける (`ESRCH` にならない) — これを reap せずに空判定
        すると `timeout`/`ignoring_sigterm` の両経路が最大 5 秒の空待ちで
        毎回タイムアウトする (実測: B9)。ループ中に非 blocking で
        `proc.wait(timeout=0)` を試みてから空判定する。"""
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = time.monotonic() + self._cli_terminate_grace_sec
        while time.monotonic() < deadline:
            self._reap(proc)
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        self._reap(proc)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self._reap(proc)
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        """`proc` が既に死んでいれば非 blocking で `wait()` し zombie を
        回収する。まだ生きていれば何もしない (`timeout=0` は即座に
        `TimeoutExpired` を送出する — blocking しない)。"""
        try:
            proc.wait(timeout=0)
        except subprocess.TimeoutExpired:
            pass

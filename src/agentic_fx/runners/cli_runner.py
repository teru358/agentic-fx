"""CliRunner — claude / codex 共通基盤 (設計書 §1.1)。

個別 backend (ClaudeRunner/CodexRunner) は argv/env の組み立てと最終出力の
抽出だけをオーバーライドする。子プロセスは常に `runners.launcher` 経由で
起動する (multi-threaded な mission_worker で `preexec_fn` を使わない —
Global Constraints)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import jsonschema

from agentic_fx._safe_error import safe_text
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.runners.launcher import build_launcher_argv
from agentic_fx.tools.registry import ToolRegistry

_MAX_REASON_CHARS = 500

# 段A (mission transcript 常時保存): 既定の保存先。`<repo>/logs/` は
# gitignore 済み (.gitignore:18)。`__file__` から repo root を導く形は
# `mission_worker._guarded_data_dir` と同じ流儀 — 呼び出し元 (workdir) の
# 事情に左右されずに固定できる。**モジュール属性として** 保持し、
# `_save_transcript` は毎回この属性を読み直す — テストは
# `tests/conftest.py` のセッション fixture でこの属性そのものを
# monkeypatch し、実リポジトリの `logs/`を一切触らせない
# (`tests-touching-real-repo-resources` の再演防止)。
_TRANSCRIPT_DIR_DEFAULT = Path(__file__).resolve().parents[3] / "logs" / "mission-transcripts"

#: stdout イベント行の合計サイズがこれを超えたら先頭/末尾を残して中間を
#: 省略する (肥大化対策)。テストが差し替えられるようモジュール定数にする。
_TRANSCRIPT_MAX_BYTES = 50 * 1024 * 1024

#: stdout 予算 (下記 `_effective_transcript_max_bytes` の結果) のうち
#: stderr tail に残す割合。マーカー/JSON overhead 込みでも合計が予算を
#: 大きく超えないための目安。
_STDERR_TAIL_BUDGET_FRACTION = 0.15


def _effective_transcript_max_bytes() -> int:
    """`_TRANSCRIPT_MAX_BYTES` (既定 50MB) と、いま実際に効いている
    `RLIMIT_FSIZE` の小さい方を返す。

    mission_worker (改善 loop・trade loop 双方) は `_set_resource_limits`
    で `RLIMIT_FSIZE` を soft=hard で固定する (既定 `child_fsize_mb: 8`
    — `config/settings.yaml.example`)。これを超えて `write()` すると
    `SIGXFSZ` (既定動作: 即座にプロセスを終了、`try/except` で捕捉
    **できない**) が飛ぶ — 「transcript 保存の失敗で mission を落とさない」
    という制約 (§4) は Python 例外だけでは満たせず、書く前に上限側で
    クランプする必要がある。安全マージンとして soft limit の半分までに
    抑える (JSON overhead・`_stderr_tail` 行・ファイルシステムの
    ブロック丸め等の余地を残す)。"""
    cap = _TRANSCRIPT_MAX_BYTES
    try:
        import resource
        soft, _hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        if soft not in (resource.RLIM_INFINITY, -1) and soft >= 0:
            cap = min(cap, max(int(soft * 0.5), 1024))
    except (ImportError, ValueError, OSError):
        pass
    return cap


def _budget_transcript_lines(stdout_lines: list[str], max_bytes: int) -> list[str]:
    """`stdout_lines` の合計サイズが `max_bytes` を超える場合、先頭と末尾を
    それぞれ半分の予算まで残し、中間を省略した旨の 1 行を挟んで返す。
    超えなければそのまま返す。"""
    def _size(line: str) -> int:
        return len(line.encode("utf-8", "replace")) + 1

    total = sum(_size(line) for line in stdout_lines)
    if total <= max_bytes:
        return list(stdout_lines)

    half = max_bytes // 2
    head: list[str] = []
    head_bytes = 0
    for line in stdout_lines:
        b = _size(line)
        if head_bytes + b > half:
            break
        head.append(line)
        head_bytes += b

    tail: list[str] = []
    tail_bytes = 0
    for line in reversed(stdout_lines):
        b = _size(line)
        if tail_bytes + b > half:
            break
        tail.append(line)
        tail_bytes += b
    tail.reverse()

    omitted = max(len(stdout_lines) - len(head) - len(tail), 0)
    marker = json.dumps({
        "_omitted": (f"{omitted} lines omitted (~{total} bytes total, "
                      f"exceeds {max_bytes} byte cap)")})
    return head + [marker] + tail


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
                 popen: Callable[..., subprocess.Popen] | None = None,
                 transcript_dir: Path | None = None) -> None:
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
        # 段A: 既定 None なら `_save_transcript` が呼び出し時点で
        # `_TRANSCRIPT_DIR_DEFAULT` を読む (固定値をここでキャッシュしない
        # — テストの monkeypatch がインスタンス生成後でも効くようにする)。
        self._transcript_dir = transcript_dir

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
            # 段A: どの終端経路 (timeout/failed/schema mismatch/completed)
            # でも漏れなく保存する — reader thread の join 直後・全 return
            # より前のこの `finally` に置く (先行する `_terminate_pgid` が
            # 例外を出しても実行される)。
            self._save_transcript(list(stdout_lines), stderr_chunks)

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

    def _save_transcript(self, stdout_lines: list[str],
                          stderr_chunks: list[str]) -> None:
        """mission 終了時に stdout のイベント行を常時保存する (段A)。

        成功/失敗を問わず呼ばれる。保存自体の失敗 (ディスク・権限・
        Landlock で `logs/` へ書けない等) で mission を落としてはならない
        — 例外は握って `logging.warning` に留める。"""
        try:
            target_dir = (self._transcript_dir if self._transcript_dir is not None
                          else _TRANSCRIPT_DIR_DEFAULT)
            target_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            model_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", self._model) or "model"
            path = target_dir / f"{ts}-{type(self).__name__}-{model_slug}.jsonl"
            effective_cap = _effective_transcript_max_bytes()
            stderr_budget = max(int(effective_cap * _STDERR_TAIL_BUDGET_FRACTION), 256)
            stdout_budget = max(effective_cap - stderr_budget, 1024)
            lines = _budget_transcript_lines(stdout_lines, stdout_budget)
            stderr_text = "".join(stderr_chunks)
            stderr_bytes = stderr_text.encode("utf-8", "replace")
            if len(stderr_bytes) > stderr_budget:
                # 文字境界を壊さないよう decode(errors="ignore") で丸める
                # (末尾優先 — "tail" の名の通り)。
                stderr_text = stderr_bytes[-stderr_budget:].decode("utf-8", "ignore")
            with path.open("w", encoding="utf-8", errors="replace") as f:
                for line in lines:
                    f.write(line)
                    f.write("\n")
                f.write(json.dumps({"_stderr_tail": stderr_text}))
                f.write("\n")
        except Exception as e:  # noqa: BLE001 — 保存失敗で mission を落とさない
            logging.warning(
                "mission transcript の保存に失敗した (mission は継続): %s",
                safe_text(str(e)))

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

"""CliRunner 共通基盤 (設計書 §1.1、§8.1-3)。

claude/codex 共通の子プロセス起動・timeout/killpg・出力正規化・env 完全
指定を検証する。個別 backend (ClaudeRunner/CodexRunner) の argv/env 組立
差異は Task 2/3 の契約テストで検証する — ここでは `CliRunner` 自体の
骨格 (pgid ownership・timeout・reason 安全化・schema 検証) のみを扱う。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentic_fx.runners.base import Mission
from agentic_fx.runners.cli_runner import CliLaunchSpec, CliRunner
from agentic_fx.tools.registry import ToolRegistry

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
          "required": ["answer"]}


def _mission(**over: Any) -> Mission:
    d = dict(prompt="p", tools=[], output_schema=SCHEMA, max_turns=8,
             timeout_sec=5)
    d.update(over)
    return Mission(**d)


class _FakeCliRunner(CliRunner):
    """テスト専用の最小具象実装 — argv[0] にテストスクリプトを直接置く。"""

    def __init__(self, *, script: str, **kw: Any) -> None:
        self._script = script
        super().__init__(**kw)

    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]:
        return [sys.executable, "-c", self._script]

    def _build_env(self, mission: Mission) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin"}

    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict | None:
        for line in reversed(stdout_lines):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and "answer" in obj:
                return obj
        return None

    def _max_turns_semantics(self):
        return "passthrough"


_SLEEP_FOREVER = "import time\ntime.sleep(600)\n"
_SLEEP_AND_SPAWN_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "time.sleep(600)\n"
)
_PRINT_ANSWER_AND_EXIT = "import json\nprint(json.dumps({'answer': 4}))\n"
_IGNORE_SIGTERM_SLEEP = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(600)\n"
)


def _new_runner(script: str, workdir: Path, **over: Any) -> _FakeCliRunner:
    kw = dict(bin_path=Path(sys.executable), model="m", workdir=workdir,
              cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    kw.update(over)
    return _FakeCliRunner(script=script, **kw)


def _pgid_of(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def _no_process_group_members(pgid: int) -> bool:
    """`pgid` を持つ生存プロセスが 0 件であることを `ps` 相当で確認する。"""
    try:
        os.killpg(pgid, 0)
        return False  # まだ届く = 生存プロセスがある
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def test_cli_runner_completed_leaves_no_pgid_survivors(tmp_path):
    """§8.1-3: completed 終端で CLI の pgid が空になる。

    <!-- precheck 2026-08-22: T1-M15 --> Minor 15 の再発防止: 旧稿は
    `spying_popen` を使わない `runner` (未使用) と `runner_with_spy` の
    2 つを作っていた (実装者が迷う死んだコード)。`popen=spying_popen` を
    渡す 1 つだけを作る。
    """
    seen_pgid: dict[str, int] = {}

    def on_message(frame: dict) -> None:
        pass

    # pgid を観測するために popen をラップする
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen_pgid["pgid"] = os.getpgid(p.pid)
        return p

    runner_with_spy = _new_runner(
        _PRINT_ANSWER_AND_EXIT, tmp_path, on_message=on_message, popen=spying_popen)
    result = runner_with_spy.run(_mission())
    assert result.status == "completed"
    assert result.output == {"answer": 4}
    assert "pgid" in seen_pgid
    assert _no_process_group_members(seen_pgid["pgid"])


def test_cli_runner_timeout_kills_pgid_including_grandchildren(tmp_path):
    """§8.1-3: timeout 終端で CLI の pgid (と synthetic な孫連鎖) が空になる。"""
    seen_pgid: dict[str, int] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen_pgid["pgid"] = os.getpgid(p.pid)
        return p

    runner = _new_runner(_SLEEP_AND_SPAWN_GRANDCHILD, tmp_path, popen=spying_popen)
    result = runner.run(_mission(timeout_sec=0.5))
    assert result.status == "timeout"
    time.sleep(0.5)  # 孫プロセスの spawn 猶予
    assert _no_process_group_members(seen_pgid["pgid"])


def test_cli_runner_ignoring_sigterm_still_reaches_sigkill_and_empties_pgid(tmp_path):
    """§8.1-3: SIGTERM を無視する CLI でも grace 後 SIGKILL で pgid が空になる。"""
    seen_pgid: dict[str, int] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen_pgid["pgid"] = os.getpgid(p.pid)
        return p

    runner = _new_runner(_IGNORE_SIGTERM_SLEEP, tmp_path, popen=spying_popen)
    start = time.monotonic()
    result = runner.run(_mission(timeout_sec=0.3))
    elapsed = time.monotonic() - start
    assert result.status == "timeout"
    assert elapsed < 5.0, "SIGKILL エスカレーションが起きていない (grace で無限待ち)"
    assert _no_process_group_members(seen_pgid["pgid"])


def test_cli_runner_cli_pgid_differs_from_worker_process_group(tmp_path):
    """CLI は自分専用の pgid に置かれる (`start_new_session=True`) —
    `killpg` が mission_worker 自身 (このテストプロセス) を殺さない。"""
    seen_pgid: dict[str, int] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen_pgid["pgid"] = os.getpgid(p.pid)
        return p

    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path, popen=spying_popen)
    runner.run(_mission())
    assert seen_pgid["pgid"] != os.getpgid(os.getpid())


def test_cli_runner_timeout_priority_over_max_turns(tmp_path):
    """timeout_sec が常に優先 (§1.5)。長時間実行 CLI は max_turns の
    passthrough に関わらず timeout で終端する。"""
    runner = _new_runner(_SLEEP_FOREVER, tmp_path)
    result = runner.run(_mission(timeout_sec=0.3, max_turns=999))
    assert result.status == "timeout"


def test_cli_runner_schema_mismatch_is_failed(tmp_path):
    """出力が output_schema に不適合なら failed (§1.1-6)。"""
    script = "import json\nprint(json.dumps({'wrong_key': 1}))\n"
    runner = _new_runner(script, tmp_path)
    result = runner.run(_mission())
    assert result.status == "failed"
    assert result.reason is not None


def test_cli_runner_does_not_use_subprocess_devnull_for_stdin(tmp_path):
    """`subprocess.DEVNULL` は使わない (probe §5-③: Landlock 下で `/dev`
    が ro だと `O_RDWR` オープンが失敗する) — `open('/dev/null', O_RDONLY)`
    相当の fd を渡すことを popen 呼び出しの kwargs で pin する。

    <!-- precheck 2026-08-22: T1-M11 --> Minor 11 の再発防止: `!=
    subprocess.DEVNULL` だけだと `stdin=None` (継承) へ変異しても green の
    まま (弱い pin)。int fd であること・`O_RDONLY` で開かれた character
    device であることまで見る。
    """
    import stat as _stat

    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        # `CliRunner.run` は Popen 呼び出し直後の `finally` で自分の
        # devnull fd を close する — fstat は fd がまだ有効なこの
        # spy の内側で行う (B7 と同じ「観測は観測できる瞬間に行う」規律)。
        stdin_fd = kw.get("stdin")
        if isinstance(stdin_fd, int) and stdin_fd >= 0:
            st = os.fstat(stdin_fd)
            captured["stdin_is_chr"] = _stat.S_ISCHR(st.st_mode)
        captured["stdin"] = stdin_fd
        return real_popen(*a, **kw)

    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path, popen=spying_popen)
    runner.run(_mission())
    stdin_fd = captured.get("stdin")
    assert stdin_fd != subprocess.DEVNULL
    assert isinstance(stdin_fd, int) and stdin_fd >= 0, (
        f"stdin は継承 (None) でも DEVNULL でもなく明示 fd でなければ"
        f"ならない: {stdin_fd!r}")
    assert captured.get("stdin_is_chr") is True, (
        "stdin fd が character device (/dev/null) でない")


def test_cli_runner_env_is_fully_specified_no_secret_keys(tmp_path):
    """env に鍵の名前を含む変数がゼロ (§1.1-3 の pin 規律)。"""
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        captured.update(kw)
        return real_popen(*a, **kw)

    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path, popen=spying_popen)
    runner.run(_mission())
    env = captured["env"]
    assert all("API_KEY" not in k and "ANTHROPIC" not in k and "OPENAI" not in k
               for k in env)


def test_cli_runner_reason_is_single_line_and_capped(tmp_path):
    """reason 安全化 (§1.1-7): 単一行・秘密除去・外部応答本文を生で入れない。"""
    script = ("import sys\n"
              "sys.stderr.write('line one\\nAPIKEY=SECRET123\\n' * 50)\n"
              "sys.exit(1)\n")
    runner = _new_runner(script, tmp_path)
    result = runner.run(_mission())
    assert result.status == "failed"
    assert "\n" not in result.reason
    assert "SECRET123" not in result.reason
    assert len(result.reason) <= 500


def test_cli_runner_uses_launcher_not_preexec_fn(tmp_path):
    """`CliRunner.run` の Popen 呼び出しに `preexec_fn` を渡さない構造 pin。"""
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        captured.update(kw)
        return real_popen(*a, **kw)

    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path, popen=spying_popen)
    runner.run(_mission())
    assert "preexec_fn" not in captured or captured["preexec_fn"] is None


def test_cli_runner_calls_cli_started_sink_with_cli_pgid(tmp_path):
    """<!-- precheck 2026-08-22: T1-B10 --> §7.1-2 の blocking 受入条件の
    送出側: Popen 直後に `cli_started_sink(pgid)` を 1 回呼ぶ (mission_worker
    が out_seq 経由で `{"type":"cli_started","pgid":...}` を親へ転送する
    送出点)。"""
    seen: list[int] = []
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path,
                         cli_started_sink=seen.append)
    runner.run(_mission())
    assert len(seen) == 1
    assert isinstance(seen[0], int)


def test_cli_runner_cli_started_sink_is_optional(tmp_path):
    """`cli_started_sink=None` (既定) でも `run()` は例外を出さない。"""
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path)
    result = runner.run(_mission())
    assert result.status == "completed"


def test_cli_runner_forwards_rlimits_to_launcher(tmp_path):
    """<!-- precheck 2026-08-22: T1-M8 --> Minor 8: `rlimits=` はサブ
    クラスから CLI 子プロセスへ rlimit を掛ける口として `launcher` seam へ
    転送されるだけ (`CliRunner` 自身は解釈しない — 適用は
    `runners.launcher.build_launcher_argv` の責務)。Task 2/3 は既定 `None`
    のまま使う (CLI に rlimit は掛けない、裁定 Minor 8)。"""
    captured: dict[str, Any] = {}

    def fake_launcher(expected_parent_pid, argv, *, rlimits=None):
        captured["rlimits"] = rlimits
        return argv  # 素通し (実プロセスは launcher を経由せず直接 argv を exec)

    want = {"RLIMIT_FSIZE": (1024, 1024)}
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path,
                         rlimits=want, launcher=fake_launcher)
    runner.run(_mission())
    assert captured["rlimits"] == want

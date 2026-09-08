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
import threading
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
_SPAWN_GRANDCHILD_THEN_ANSWER = (
    "import json, subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "print(json.dumps({'answer': 4}))\n"
)
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


def test_cli_runner_abort_returns_failed_with_tool_budget_reason(tmp_path):
    abort_event = threading.Event()
    abort_event.set()
    runner = _new_runner(
        _SLEEP_FOREVER, tmp_path, abort_event=abort_event,
        abort_reason_fn=lambda: "tool_budget_abort:terminal_refusals")
    result = runner.run(_mission(timeout_sec=5))
    assert result.status == "failed"
    assert result.reason == "tool_budget_abort:terminal_refusals"


def test_cli_runner_abort_uses_recovery_and_marks_recovered(tmp_path):
    class RecoveringRunner(_FakeCliRunner):
        def _run_cli_process(self, argv, env, *, timeout_sec,
                             on_started=None, abort_event=None):
            assert abort_event is event
            return "abort", None, ["partial"], []
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            assert stdout_lines == ["partial"]
            return {"answer": 4}

    event = threading.Event()
    runner = RecoveringRunner(
        script="", bin_path=Path(sys.executable), model="m", workdir=tmp_path,
        cli_terminate_grace_sec=0.3, registry=ToolRegistry(),
        abort_event=event)
    result = runner.run(_mission())
    assert result.status == "completed"
    assert result.output == {"answer": 4}
    assert result.recovered is True


def test_cli_runner_completed_with_grandchild_still_reaps_pgid_quickly(tmp_path):
    """段 0 M2 pin: `proc.poll()` (`cli_runner.py:142`) が completed 経路で
    CLI 自身を先に reap しても、`_terminate_pgid` は `killpg(pgid, ...)` で
    グループ全体 (孫を含む) を回収する — `proc` の生死を経由しない。CLI
    自身は即座に exit するが、孫 (同じ pgid で背景に残る sleep) を
    spawn してから answer を print する fake CLI で completed 経路を踏む。

    段 0 変異スイープ (stage0-bundle-A.md §2.1) の指摘: 既存の
    `test_cli_runner_completed_leaves_no_pgid_survivors` は孫を作らない
    fake CLI しか使っておらず、「completed でも孫を確実に回収する」という
    `cli_runner.py:146` のコメントの主張を検証できていなかった。`_terminate_pgid`
    の `os.killpg(pgid, signal.SIGTERM)` を `os.kill(proc.pid, signal.SIGTERM)`
    (シグナル送出先をプロセス単体に落とす変異) に変えると、`proc` は
    `proc.poll()` で既に reap 済みのため `ProcessLookupError` → 早期
    `return` に落ち、孫が回収されずに `t_out.join(5.0)`/`t_err.join(5.0)`
    まで待たされる (約 10 秒) — 変異ありなしの両方で `status == "completed"`
    のまま区別が付かないため、`elapsed` と孫の生存有無の両方を assert する。
    """
    seen_pgid: dict[str, int] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen_pgid["pgid"] = os.getpgid(p.pid)
        return p

    runner = _new_runner(
        _SPAWN_GRANDCHILD_THEN_ANSWER, tmp_path, popen=spying_popen)
    start = time.monotonic()
    result = runner.run(_mission())
    elapsed = time.monotonic() - start
    assert result.status == "completed"
    assert result.output == {"answer": 4}
    assert "pgid" in seen_pgid
    assert _no_process_group_members(seen_pgid["pgid"]), (
        "completed 経路で孫プロセスが pgid に生存している (M2)")
    assert elapsed < 2.0, (
        f"孫の回収が早期 return で skip され reader thread の join(5.0)×2 "
        f"待ちに落ちている (実測 {elapsed:.2f}s)")


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


def test_cli_runner_terminate_pgid_sends_sigterm_before_sigkill(tmp_path):
    """#15 (`verified-round1.md` 1-A): `_terminate_pgid` は SIGKILL 到達前に
    まず SIGTERM を送る。現行の 3 本 (completed/timeout 系) は「最終的に
    pgid が空になる」ことしか見ないため、`_terminate_pgid` 冒頭の
    `try: os.killpg(pgid, SIGTERM) except ...: return` を削除して
    SIGKILL のみにする変異でも SURVIVED する (実測: `stage0`/`round1`)。
    SIGTERM を捕捉して marker ファイルを書いてから自発終了する fake CLI
    を使い、「SIGKILL に到達する前に SIGTERM で終わった」ことを marker の
    存在で観測する (grace を長めに取り、marker が grace 内に現れることを
    elapsed で確認する = 順序 pin)。"""
    marker = tmp_path / "sigterm_marker"
    script = (
        "import signal, sys, time\n"
        f"MARKER = {str(marker)!r}\n"
        "def _h(signum, frame):\n"
        "    open(MARKER, 'w').write('term')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _h)\n"
        "time.sleep(600)\n"
    )
    runner = _new_runner(script, tmp_path, cli_terminate_grace_sec=5.0)
    start = time.monotonic()
    result = runner.run(_mission(timeout_sec=0.3))
    elapsed = time.monotonic() - start
    assert result.status == "timeout"
    assert marker.is_file(), (
        "SIGTERM ハンドラが marker を書く前に終端している — SIGKILL が "
        "SIGTERM より先/代わりに送られている可能性")
    assert marker.read_text() == "term"
    assert elapsed < 5.0, (
        "grace (5.0s) の SIGKILL タイムアウトまで待たされている — "
        "SIGTERM で早期終了できていない")


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
            # #22 (`verified-round1.md` 1-B): 「character device かつ
            # O_RDONLY」までは character device であれば任意 (`/dev/zero`
            # 等) でも通っていた。fd の実体が `/dev/null` そのものであること
            # まで見る。
            captured["stdin_target"] = os.readlink(f"/proc/self/fd/{stdin_fd}")
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
    assert captured.get("stdin_target") == "/dev/null", (
        f"stdin fd の実体が /dev/null ではない: {captured.get('stdin_target')!r}")


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
    # #17 (`verified-round1.md` 1-B): キー名部分一致だけだと値に秘密を
    # 入れる変異・新しい秘密名 (`*_TOKEN` 等) が素通りする。
    # `_CLAUDE/_CODEX_ENV_ALLOWLIST` と同じ**集合一致**形に寄せる
    # (`_FakeCliRunner._build_env` は `{"PATH": "/usr/bin:/bin"}` のみ)。
    assert set(env) == {"PATH"}
    assert env["PATH"] == "/usr/bin:/bin"


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
    送出点)。

    #19 (`verified-round1.md` 1-B): `cli_started_sink(pgid)` を
    `cli_started_sink(proc.pid)` に変えても通っていた (`start_new_session=True`
    により両者は一致するため実害は無いが、その前提が pin されていなかった)。
    spy で `proc.pid` を捕らえ、`seen[0] == os.getpgid(proc.pid)` を assert する。"""
    seen: list[int] = []
    seen_pgid: dict[str, int] = {}
    real_popen = subprocess.Popen

    def spying_popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen_pgid["pgid"] = os.getpgid(p.pid)  # spy 時点 (プロセス生存中) で観測
        return p

    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path,
                         cli_started_sink=seen.append, popen=spying_popen)
    runner.run(_mission())
    assert len(seen) == 1
    assert isinstance(seen[0], int)
    assert seen[0] == seen_pgid["pgid"]


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


# --- 段A: mission transcript 常時保存 --------------------------------


def test_cli_runner_saves_transcript_on_completed_mission(tmp_path):
    """成功 mission でも transcript ファイルが生成され、stdout のイベント
    行がそのまま入っている。"""
    transcript_dir = tmp_path / "transcripts"
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path,
                         transcript_dir=transcript_dir)
    result = runner.run(_mission())
    assert result.status == "completed"
    files = list(transcript_dir.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert any(json.loads(l).get("answer") == 4 for l in lines
               if l and not l.startswith('{"_'))


def test_cli_runner_saves_transcript_when_no_output_recovered(tmp_path):
    """出力回収失敗 (no output) の failed mission でも transcript は
    生成される。"""
    transcript_dir = tmp_path / "transcripts"
    script = "print('not json and no answer key')\n"
    runner = _new_runner(script, tmp_path, transcript_dir=transcript_dir)
    result = runner.run(_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason
    files = list(transcript_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert "not json and no answer key" in files[0].read_text(encoding="utf-8")


def test_cli_runner_saves_transcript_on_timeout(tmp_path):
    """timeout 終端でも transcript の保存呼び出しは通る (保存箇所の
    placement pin — join 直後の finally を素通りしていないか)。"""
    transcript_dir = tmp_path / "transcripts"
    runner = _new_runner(_SLEEP_FOREVER, tmp_path, transcript_dir=transcript_dir)
    result = runner.run(_mission(timeout_sec=0.3))
    assert result.status == "timeout"
    files = list(transcript_dir.glob("*.jsonl"))
    assert len(files) == 1


def test_cli_runner_transcript_dir_is_auto_created(tmp_path):
    """保存先ディレクトリが存在しなくても自動作成される (親も含む)。"""
    transcript_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not transcript_dir.exists()
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path,
                         transcript_dir=transcript_dir)
    runner.run(_mission())
    assert transcript_dir.is_dir()
    assert len(list(transcript_dir.glob("*.jsonl"))) == 1


def test_cli_runner_transcript_save_failure_does_not_raise_and_result_returned(tmp_path):
    """保存先が書込不可 (権限エラー) でも `run()` は例外を出さず
    MissionResult を返す。"""
    transcript_dir = tmp_path / "ro_transcripts"
    transcript_dir.mkdir()
    transcript_dir.chmod(0o500)  # 書込不可、読み+実行のみ
    try:
        runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path,
                             transcript_dir=transcript_dir)
        result = runner.run(_mission())
        assert result.status == "completed"
        assert result.output == {"answer": 4}
        assert list(transcript_dir.glob("*.jsonl")) == []
    finally:
        transcript_dir.chmod(0o700)  # tmp_path クリーンアップのため復元


def test_cli_runner_transcript_stderr_tail_is_appended(tmp_path):
    """stderr も末尾に `{"_stderr_tail": "..."}` 行として付く。"""
    transcript_dir = tmp_path / "transcripts"
    script = ("import sys, json\n"
              "sys.stderr.write('diag line\\n')\n"
              "print(json.dumps({'answer': 4}))\n")
    runner = _new_runner(script, tmp_path, transcript_dir=transcript_dir)
    runner.run(_mission())
    files = list(transcript_dir.glob("*.jsonl"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    stderr_lines = [json.loads(l) for l in lines if '"_stderr_tail"' in l]
    assert len(stderr_lines) == 1
    assert "diag line" in stderr_lines[0]["_stderr_tail"]


def test_cli_runner_transcript_truncates_when_over_budget(tmp_path, monkeypatch):
    """肥大化対策: 合計サイズが上限を超えると先頭/末尾を残して中間を
    `_omitted` マーカー付きで省略する。上限を極小に monkeypatch して
    実測する。"""
    from agentic_fx.runners import cli_runner as cli_runner_mod

    monkeypatch.setattr(cli_runner_mod, "_TRANSCRIPT_MAX_BYTES", 200)
    transcript_dir = tmp_path / "transcripts"
    # 200 バイト上限を確実に超える量の event 行を吐く fake CLI。
    script = (
        "import json\n"
        "for i in range(200):\n"
        "    print(json.dumps({'i': i, 'pad': 'x' * 20}))\n"
        "print(json.dumps({'answer': 4}))\n"
    )
    runner = _new_runner(script, tmp_path, transcript_dir=transcript_dir)
    result = runner.run(_mission())
    assert result.status == "completed"
    files = list(transcript_dir.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    omitted_lines = [l for l in lines if '"_omitted"' in l]
    assert len(omitted_lines) == 1
    # 先頭 (i=0) と末尾 (answer=4) の両方が残っている。
    assert any('"i": 0' in l for l in lines[:5])
    assert any(json.loads(l).get("answer") == 4 for l in lines
               if l and not l.startswith('{"_'))
    # 中間は本当に間引かれている (全 201 行 + マーカー + stderr_tail より
    # 少ない)。
    assert len(lines) < 203


# --- 段B: 追撃回収フック (_recover_output) の基底 no-op 契約 -----------


def test_cli_runner_recover_output_default_is_noop(tmp_path):
    """基底 `CliRunner._recover_output` は常に None (no-op)。追撃を実装
    しない backend (_FakeCliRunner) の timeout/no-output 挙動が変わらない
    ことの直接 pin。"""
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path)
    assert runner._recover_output(_mission(), ["some", "stdout", "lines"], 0.0) is None
    assert runner._recovery_reserve_sec() == 0.0


def test_cli_runner_timeout_path_invokes_recover_output_hook(tmp_path):
    """timeout 経路は return 前に `_recover_output(mission, stdout_lines,
    recovery_timeout_sec)` を呼ぶ — 戻り値が None でなければ通常の schema
    検証経路 (completed) に乗る。"""
    calls: list[tuple[Any, list[str], float]] = []

    class _RecoveringRunner(_FakeCliRunner):
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            calls.append((mission, list(stdout_lines), recovery_timeout_sec))
            return {"answer": 4}

    runner = _RecoveringRunner(
        script=_SLEEP_FOREVER, bin_path=Path(sys.executable), model="m",
        workdir=tmp_path, cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    result = runner.run(_mission(timeout_sec=0.3))
    assert result.status == "completed"
    assert result.output == {"answer": 4}
    assert len(calls) == 1


def test_cli_runner_no_output_path_invokes_recover_output_hook(tmp_path):
    """no-output (`_extract_output` が None) 経路も return 前に
    `_recover_output` を呼ぶ。戻り値があれば completed に乗る。"""
    calls: list[tuple[Any, list[str], float]] = []

    class _RecoveringRunner(_FakeCliRunner):
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            calls.append((mission, list(stdout_lines), recovery_timeout_sec))
            return {"answer": 4}

    script = "print('no answer key here')\n"
    runner = _RecoveringRunner(
        script=script, bin_path=Path(sys.executable), model="m",
        workdir=tmp_path, cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    result = runner.run(_mission())
    assert result.status == "completed"
    assert result.output == {"answer": 4}
    assert len(calls) == 1


def test_cli_runner_recover_output_returning_none_preserves_original_failed_reason(tmp_path):
    """`_recover_output` が None を返す (回収できず) 場合、no-output 経路の
    reason は追撃なし時と同一のまま (基底 no-op と完全一致)。"""
    script = "print('no answer key here')\n"
    runner = _new_runner(script, tmp_path)
    result = runner.run(_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason


def test_cli_runner_recover_output_success_sets_recovered_true(tmp_path):
    """M4: `_recover_output` の追撃が成功した (非 None かつ schema 検証を
    通った) completed には `recovered=True` が立つ。"""
    class _RecoveringRunner(_FakeCliRunner):
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            return {"answer": 4}

    runner = _RecoveringRunner(
        script=_SLEEP_FOREVER, bin_path=Path(sys.executable), model="m",
        workdir=tmp_path, cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    result = runner.run(_mission(timeout_sec=0.3))
    assert result.status == "completed"
    assert result.recovered is True


def test_cli_runner_primary_success_leaves_recovered_false(tmp_path):
    """M4 対照: primary (追撃なし) の completed は `recovered=False`
    (既定) のまま — recovered を常に True にする変異を殺す。"""
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path)
    result = runner.run(_mission())
    assert result.status == "completed"
    assert result.recovered is False


def test_cli_runner_reserve_zero_primary_timeout_is_full_mission_timeout(tmp_path):
    """段B M2 pin: `_recovery_reserve_sec()` が 0 (基底既定) のとき、
    `_run_cli_process` に渡る primary の timeout は `mission.timeout_sec`
    そのまま (reserve を差し引かない) — 現状の timeout 挙動と完全一致する
    ことを `_run_cli_process` の呼び出し引数で直接 pin する。"""
    captured: dict[str, Any] = {}

    class _SpyingRunner(_FakeCliRunner):
        def _run_cli_process(self, argv, env, *, timeout_sec, on_started=None,
                             abort_event=None):
            captured["timeout_sec"] = timeout_sec
            return super()._run_cli_process(
                argv, env, timeout_sec=timeout_sec, on_started=on_started,
                abort_event=abort_event)

    runner = _SpyingRunner(
        script=_PRINT_ANSWER_AND_EXIT, bin_path=Path(sys.executable), model="m",
        workdir=tmp_path, cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    runner.run(_mission(timeout_sec=5))
    assert captured["timeout_sec"] == 5


def test_cli_runner_reserve_positive_shortens_primary_timeout(tmp_path):
    """段B M2 pin: `_recovery_reserve_sec() > 0` かつ
    `mission.timeout_sec > reserve` のとき、primary の timeout は
    `mission.timeout_sec - reserve` に短縮され、追撃には `reserve` 秒が
    予算として渡る。"""
    captured: dict[str, Any] = {}
    recover_calls: list[float] = []

    class _ReservingRunner(_FakeCliRunner):
        def _recovery_reserve_sec(self):
            return 2.0

        def _run_cli_process(self, argv, env, *, timeout_sec, on_started=None,
                             abort_event=None):
            captured["timeout_sec"] = timeout_sec
            return super()._run_cli_process(
                argv, env, timeout_sec=timeout_sec, on_started=on_started,
                abort_event=abort_event)

        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            recover_calls.append(recovery_timeout_sec)
            return None

    script = "print('no answer key here')\n"
    runner = _ReservingRunner(
        script=script, bin_path=Path(sys.executable), model="m",
        workdir=tmp_path, cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    runner.run(_mission(timeout_sec=5))
    assert captured["timeout_sec"] == 3
    assert recover_calls == [2.0]


def test_cli_runner_reserve_disabled_when_mission_timeout_not_greater_than_reserve(tmp_path):
    """段B M2 pin: `mission.timeout_sec <= reserve` のときは reserve を
    無効化し、primary に全予算 (`mission.timeout_sec`) を渡す。追撃予算は
    0 になる (`_recover_output` に 0.0 が渡る)。"""
    captured: dict[str, Any] = {}
    recover_calls: list[float] = []

    class _ReservingRunner(_FakeCliRunner):
        def _recovery_reserve_sec(self):
            return 10.0

        def _run_cli_process(self, argv, env, *, timeout_sec, on_started=None,
                             abort_event=None):
            captured["timeout_sec"] = timeout_sec
            return super()._run_cli_process(
                argv, env, timeout_sec=timeout_sec, on_started=on_started,
                abort_event=abort_event)

        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            recover_calls.append(recovery_timeout_sec)
            return None

    script = "print('no answer key here')\n"
    runner = _ReservingRunner(
        script=script, bin_path=Path(sys.executable), model="m",
        workdir=tmp_path, cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    runner.run(_mission(timeout_sec=5))  # 5 <= reserve(10)
    assert captured["timeout_sec"] == 5
    assert recover_calls == [0.0]


def test_cli_runner_transcript_default_dir_is_module_attribute(tmp_path, monkeypatch):
    """`transcript_dir=None` (既定) のとき、`_TRANSCRIPT_DIR_DEFAULT` を
    呼び出し時点で読む (インスタンス生成時に固定しない) — テストの
    session fixture がこの属性を monkeypatch して実リポジトリを保護できる
    ことの pin。"""
    from agentic_fx.runners import cli_runner as cli_runner_mod

    isolated = tmp_path / "isolated-default"
    monkeypatch.setattr(cli_runner_mod, "_TRANSCRIPT_DIR_DEFAULT", isolated)
    runner = _new_runner(_PRINT_ANSWER_AND_EXIT, tmp_path)  # transcript_dir 未指定
    runner.run(_mission())
    assert len(list(isolated.glob("*.jsonl"))) == 1


# --- ローカル 1 周目 pin (2026-09-08、tmp/review-20260908-ma/verified-round1-local.md) ---

# P1
def test_run_cli_process_prefers_timeout_when_deadline_and_event_coincide(tmp_path):
    """ローカル 1 周目 #1 (muse c2): poll loop は deadline を event より先に
    評価する — wall-clock 優先の意味契約 (`base.py:23,28`)。両方が同時に真の
    状態で入ると、評価順を入れ替える変異が全スイート green で生存していた
    (段 0 が red と記録していたが実測は生存)。timeout_sec=0.0 で loop 突入
    時点の deadline 到達を決定論的に作る。"""
    event = threading.Event()
    event.set()
    runner = _new_runner(_SLEEP_FOREVER, tmp_path, abort_event=event)
    cause, _rc, _out, _err = runner._run_cli_process(
        [sys.executable, "-c", _SLEEP_FOREVER], {"PATH": "/usr/bin:/bin"},
        timeout_sec=0.0, abort_event=event)
    assert cause == "timeout"

# P7
def test_abort_saves_transcript_like_timeout(tmp_path):
    """ローカル 1 周目 #8 (muse c2): abort は timeout と同じ追撃経路を通る =
    `_save_transcript` も同じ位置で呼ばれる。abort 時だけ保存を飛ばす変異が
    生存していた (既存 abort テストは status/reason しか見ていない)。"""
    saved = []

    class R(_FakeCliRunner):
        def _run_cli_process(self, argv, env, *, timeout_sec,
                             on_started=None, abort_event=None):
            return "abort", None, ["partial"], []

        def _save_transcript(self, stdout_lines, stderr_chunks):
            saved.append(list(stdout_lines))

    event = threading.Event()
    runner = R(script="", bin_path=Path(sys.executable), model="m",
               workdir=tmp_path, cli_terminate_grace_sec=0.3,
               registry=ToolRegistry(), abort_event=event,
               abort_reason_fn=lambda: "tool_budget_abort:terminal_refusals")
    result = runner.run(_mission())
    assert result.status == "failed"
    assert saved == [["partial"]]



def test_abort_recovered_completion_keeps_abort_reason(tmp_path):
    """/code-review 2 周目 #2 (2026-09-08): abort → 追撃で回収できた completed は
    abort の provenance (reason prefix) を保持する (worker の summary 付与と親の
    activity がこれを読む)。recovered=True も維持。"""
    from agentic_fx.runners.base import is_tool_budget_abort

    class RecoveringRunner(_FakeCliRunner):
        def _run_cli_process(self, argv, env, *, timeout_sec,
                             on_started=None, abort_event=None):
            return "abort", None, ["partial"], []
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            return {"answer": 4}

    event = threading.Event(); event.set()
    runner = RecoveringRunner(
        script="", bin_path=Path(sys.executable), model="m", workdir=tmp_path,
        cli_terminate_grace_sec=0.3, registry=ToolRegistry(),
        abort_event=event,
        abort_reason_fn=lambda: "tool_budget_abort:terminal_refusals")
    result = runner.run(_mission())
    assert result.status == "completed"
    assert result.recovered is True
    assert is_tool_budget_abort(result.reason)
    assert result.reason == "tool_budget_abort:terminal_refusals"

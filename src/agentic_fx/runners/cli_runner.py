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
from agentic_fx.runners.base import TOOL_BUDGET_ABORT_PREFIX, AgentRunner, Mission, MissionResult
from agentic_fx.runners.launcher import build_launcher_argv
from agentic_fx.tools.registry import ToolRegistry

_MAX_REASON_CHARS = 500
TerminationCause = Literal["completed", "timeout", "abort"]

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

#: A4 10 回目 #71 観測 B (2026-09-11): registry tool 呼び出しが 1 件も
#: 無いまま completed 終端する improve mission は「agent が自主的に
#: 観察を選んだ」場合と区別がつかない。CLI backend (claude/codex/
#: opencode) がツール基盤ごと無力化されたときの stderr 既知致命パターン
#: — 観測された codex code-mode host の SIGTRAP 連鎖 (`_stderr_tail` の
#: `error=code-mode host exited with status signal: 5 (SIGTRAP)` /
#: `code-mode host closed its stdout`) がこのタプルの初出動機。空にする
#: と検知自体が無効化される (逆変異 pin の対象)。
CLI_STDERR_FATAL_PATTERNS: tuple[str, ...] = (
    "code-mode host",
    "SIGTRAP",
    "closed its stdout",
    "Segmentation fault",
)

#: activity `cli_stderr_fatal` 1 行に埋め込む tail の文字数上限
#: (ブリーフ「先頭 200 字」)。
_STDERR_FATAL_TAIL_CHARS = 200


def _detect_stderr_fatal(stderr_text: str) -> str | None:
    """`stderr_text` に `CLI_STDERR_FATAL_PATTERNS` のいずれかが含まれて
    いれば `pattern=<p> tail=<先頭 200 字>` を返す。無ければ `None`。
    最初に一致したパターン (タプル順) を採用する — 複数一致は稀で、
    診断上はどれか 1 つが分かれば十分。

    `_normalize_reason` を通す (advisor 指摘、2026-09-11): `reason` と
    同じ「外部応答本文を生で入れない」規範 (`base.py` の `AgentRunner`
    docstring) がここにも適用される。生の 200 字 tail は複数行 (実 #71
    の `_stderr_tail` は改行区切り) かつ秘密混入の可能性がある
    (`test_cli_runner_reason_is_single_line_and_capped` が `reason` に
    対して pin している規律と同じ穴)。`_normalize_reason` は単一行化・
    `safe_text` によるスクラブ・上限カットを行う — 500 字上限は 200 字
    tail より広いため実質的に切り詰めない。"""
    for pattern in CLI_STDERR_FATAL_PATTERNS:
        if pattern and pattern in stderr_text:
            tail = stderr_text[:_STDERR_FATAL_TAIL_CHARS]
            return _normalize_reason(f"pattern={pattern} tail={tail}")
    return None


def _merge_stderr_fatal(primary: str | None, recovery: str | None) -> str | None:
    """codex 1 周目 I4 是正 (2026-09-11): primary (メインプロセス) と
    recovery (`_recover_output` が起動する追撃プロセス、例 resume) の
    どちらか一方または両方に検知済み fatal 文字列があれば返す。両方
    あれば `; ` で連結し、どちらの経路で落ちたかを両方とも保つ。"""
    if primary and recovery:
        return f"{primary}; {recovery}"
    return primary or recovery


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


def _write_prompt_file(workdir: Path, prompt: str) -> Path:
    """設計書 §D: mission prompt を argv に載せず stdin 経由で渡すための
    一時ファイル。`workdir/prompt.txt` を **0600 で新規作成**する
    (`O_EXCL` — 同名ファイルが既にあれば衝突として例外を送出する、
    fail closed。1 mission の workdir は 1 回の `run()` でしか使わない
    前提と一致する)。fsync は不要 — 直後に同プロセス内で開いて読むだけで、
    クラッシュ耐性は要らない (指揮者の実装依頼どおり)。"""
    path = workdir / "prompt.txt"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    return path


class CliRunner(AgentRunner):
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None,
                 rlimits: dict[str, tuple[int, int]] | None = None,
                 launcher: Callable[..., list[str]] | None = None,
                 popen: Callable[..., subprocess.Popen] | None = None,
                 transcript_dir: Path | None = None,
                 abort_event: threading.Event | None = None,
                 abort_reason_fn: Callable[[], str] | None = None) -> None:
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
        self._abort_event = abort_event
        self._abort_reason_fn = abort_reason_fn
        # codex 1 周目 I4 是正 (2026-09-11): `_recover_output` (resume を
        # 実装する backend、例 `OpencodeRunner`) が起動する追撃プロセスの
        # stderr は、primary の stderr とは別プロセスから来る — backend は
        # ここへ検知済み fatal 文字列 (`_detect_stderr_fatal` の戻り値) を
        # 書き込み、`run()` が primary 分と `; ` 連結して
        # `MissionResult.stderr_fatal` へ渡す。`_recover_output` を
        # override しない backend (既定 no-op) は触らないため None のまま。
        self._recovery_stderr_fatal: str | None = None

    @abstractmethod
    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]: ...

    @abstractmethod
    def _build_env(self, mission: Mission) -> dict[str, str]: ...

    @abstractmethod
    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict[str, Any] | None: ...

    @abstractmethod
    def _max_turns_semantics(self) -> Literal["passthrough", "ignored"]: ...

    def _recovery_reserve_sec(self) -> float:
        """段B M2: mission 総予算のうち追撃用に取り分ける秒数 (「内数化」)。
        既定 (このクラス) は 0 — 予約しない。0 のときは `run()` の予算計算が
        `reserve > 0` を素通りし、primary の timeout は `mission.timeout_sec`
        のまま・追撃には予算 0 が渡る (= 追撃を実装しない backend は現状と
        完全一致)。backend (OpencodeRunner 等) が追撃を実装する場合に
        正の秒数を返すようオーバーライドする。"""
        return 0.0

    def _recover_output(self, mission: Mission, stdout_lines: list[str],
                         recovery_timeout_sec: float) -> dict[str, Any] | None:
        """段 B: timeout / no-output の 2 経路から `run()` が呼ぶ追撃回収
        フック。既定 (このクラス) は no-op — 常に None を返し、呼び出し側の
        既存 timeout/failed 挙動を完全に保つ。backend (OpencodeRunner 等)
        が session resume 等の追撃を実装する場合にオーバーライドする。
        `recovery_timeout_sec` は `_recovery_reserve_sec()` から
        `run()` が算出した、追撃に使ってよい残り秒数の上限 (M2: 予算の
        内数化 — mission 総予算を超えて追撃しない)。"""
        return None

    def _stdin_prompt(self, mission: Mission) -> str | None:
        """設計書 §D: `_run_cli_process` へ渡す stdin 用 prompt 文字列。

        /code-review 2 周目 CR5 是正 (2026-09-12): 既定 (このクラス) は
        `mission.prompt` — argv 渡しではなく安全側 (stdin 経由、
        `/proc/<pid>/cmdline` から読めない) を base class の**不変条件**に
        する。従来の既定は `None` (devnull) で、`_build_argv` から
        `mission.prompt` を抜くことを個々の backend の自己申告に委ねて
        いた — 新しい subclass がこの hook を override し忘れると、argv に
        `mission.prompt` を積んだまま気づかれずに通る (`ClaudeRunner`/
        `CodexRunner` は現に override していたので実害は無かったが、
        「安全な既定」ではなく「安全側にした backend だけが安全」という
        構造だった)。`run()` は `_build_argv` の戻り値にまだ
        `mission.prompt` が残っていないかをこの返り値 (非 None) と対で
        検査する (fail closed — 残っていれば `ValueError`)。stdin を
        読めない CLI (`OpencodeRunner`) は明示的にこの hook を `None` へ
        override して opt-out する — その場合だけ argv 渡しのままでよい
        (`run()` 側のガードは opt-out 時は検査しない)。"""
        return mission.prompt

    def _run_cli_process(self, argv: list[str], env: dict[str, str], *,
                          timeout_sec: float,
                          on_started: Callable[[int], None] | None = None,
                          abort_event: threading.Event | None = None,
                          prompt: str | None = None,
                          ) -> tuple[TerminationCause, int | None, list[str], list[str]]:
        """launcher 経由で `argv` を起動し、pgid 管理
        (`start_new_session=True` + `_terminate_pgid`) と stdout/stderr の
        読み切りまでを行う共通経路。`run()` の主呼び出しと、追撃
        (`_recover_output` を実装する backend) の両方がこれを使う (段B M1:
        プロセス管理の統一 — 追撃も launcher / pgid 単位の
        SIGTERM→grace→SIGKILL / `self._rlimits` という同じ規律に従う)。

        戻り値は `(cause, returncode, stdout_lines, stderr_chunks)`。`cause` は
        `TerminationCause` ("completed" / "timeout" / "abort") の**文字列**で、
        真偽値ではない — 呼び出し側は `==` で比較する ("completed" は truthy)。
        """
        launcher_argv = self._build_launcher_argv(
            os.getpid(), argv, rlimits=self._rlimits)

        # 設計書 §D: `prompt` が渡されたときだけ `workdir/prompt.txt`
        # (0600) をファイル読み出しで stdin に充てる — pipe に書きながら
        # 流すのではなくファイル読み出しにすることで、prompt サイズによる
        # pipe バッファのデッドロックを避ける。渡されなければ既存どおり
        # `/dev/null` を開く (devnull と同じ「開いて Popen へ渡し、
        # Popen 後に close する」流儀をそのまま流用)。
        if prompt is not None:
            prompt_path = _write_prompt_file(self._workdir, prompt)
            stdin_r = os.open(str(prompt_path), os.O_RDONLY)
        else:
            stdin_r = os.open(os.devnull, os.O_RDONLY)
        try:
            proc = self._popen(
                launcher_argv, cwd=str(self._workdir), env=env,
                stdin=stdin_r, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, text=True)
        finally:
            os.close(stdin_r)

        pgid = os.getpgid(proc.pid)
        if on_started is not None:
            on_started(pgid)  # <!-- precheck 2026-08-22: T1-B10 --> §7.1-2
        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                stdout_lines.append(line.rstrip("\n"))
                self._on_message({"type": "event", "message": {
                    "role": "system", "content": line.rstrip("\n")}})

        def stderr_reader() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)

        t_out = threading.Thread(target=reader, daemon=True)
        t_err = threading.Thread(target=stderr_reader, daemon=True)
        t_out.start()
        t_err.start()

        cause: TerminationCause = "completed"
        deadline = time.monotonic() + timeout_sec
        while True:
            if time.monotonic() >= deadline:
                cause = "timeout"
                break
            if abort_event is not None and abort_event.is_set():
                cause = "abort"
                break
            try:
                proc.wait(timeout=min(1.0, max(deadline - time.monotonic(), 0)))
                break
            except subprocess.TimeoutExpired:
                continue

        try:
            if cause != "completed" or proc.poll() is None:
                self._terminate_pgid(pgid, proc)
            else:
                self._terminate_pgid(pgid, proc)  # completed でも孫を確実に回収する (§1.1-4)
        finally:
            t_out.join(timeout=5.0)
            t_err.join(timeout=5.0)

        return cause, proc.returncode, stdout_lines, stderr_chunks

    def run(self, mission: Mission) -> MissionResult:
        # B2-r2 是正: bind 側 (`mission_worker._start_mcp_dispatcher`) と
        # 同じ `mcp_socket_path()` から導出する — 独立したリテラルの
        # 偶然の一致に頼らない (検収 B2-r2、`cli_runner.py:88` のパス
        # 変異が全スイートを生き延びた指摘への対応)。
        from agentic_fx.mission_worker import mcp_socket_path
        mcp_socket = mcp_socket_path(self._workdir)
        inner_argv = self._build_argv(mission, mcp_socket=mcp_socket)
        env = self._build_env(mission)

        # /code-review 2 周目 CR5 是正 (2026-09-12): `_stdin_prompt` を
        # 一度だけ呼ぶ (`_run_cli_process` への引数と、このガードの両方が
        # 同じ値を見る)。既定 (stdin 経由、非 None) を選んだ subclass は
        # `_build_argv` が `mission.prompt` を argv から抜いている
        # ことを assert する — 抜けていなければ、その prompt は
        # `/proc/<pid>/cmdline` から他の同 UID プロセスに読める
        # (設計書 §D の脅威モデル) ので fail closed で `ValueError`。
        # `_stdin_prompt` を明示的に `None` へ override した opt-out
        # backend (`OpencodeRunner` — stdin 受理未確認) はこの検査の対象
        # 外 — argv 渡しのままでよい。
        #
        # codex 2 周目 I2 是正 (2026-09-12): 要素**完全一致**だけでは
        # `--prompt=<mission.prompt>` のような連結や、テンプレート接頭辞
        # 付き argv 要素 (`"prefix: " + mission.prompt`) を見逃す —
        # これらは要素として `mission.prompt` と等しくないが、prompt の
        # 全文または大部分を argv 要素の中に含んでおり同じ脅威 (`/proc/
        # <pid>/cmdline` 経由の露出) を持つ。よって argv の**各要素に
        # ついて部分文字列として** prompt 全文、または prompt が長い
        # 場合はその先頭 64 字 (短ければ全文と同じ) が含まれるかを見る。
        # 先頭 64 字を含む要素は必ず全文を含む要素の上位集合になるため
        # (prefix はその prompt 自体の部分文字列)、実装は prefix の
        # 包含だけを判定すれば両方を捕捉できる。空 prompt はそもそも
        # 情報を持たないため対象外 (`in` が常に True になり誤検知するのを
        # 防ぐ)。
        stdin_prompt = self._stdin_prompt(mission)
        prompt = mission.prompt
        if stdin_prompt is not None and prompt:
            prompt_prefix = prompt[:64]
            if any(prompt_prefix in arg for arg in inner_argv):
                raise ValueError(
                    f"{type(self).__name__}._build_argv leaves "
                    "mission.prompt (or its leading 64 chars) embedded in "
                    "an argv element while _stdin_prompt did not opt out "
                    "(None) — the prompt would be readable via "
                    "/proc/<pid>/cmdline by other same-UID processes "
                    "(design doc §D). Either remove mission.prompt from "
                    "_build_argv's return value, or override "
                    "_stdin_prompt to return None with a comment "
                    "explaining why this CLI cannot accept stdin.")

        # 段B M2: 追撃予算を mission 総予算の内数にする。reserve<=0、または
        # mission.timeout_sec が reserve 以下なら reserve を無効化し、
        # primary に全予算を渡す (追撃は予算 0 → `_recover_output` に
        # 委ねる — 基底/reserve=0 の場合は現状と完全一致)。
        reserve = self._recovery_reserve_sec()
        if reserve > 0 and mission.timeout_sec > reserve:
            primary_timeout = mission.timeout_sec - reserve
            recovery_timeout = reserve
        else:
            primary_timeout = mission.timeout_sec
            recovery_timeout = 0.0

        # codex 1 周目 I4 是正 (2026-09-11): この mission の追撃 stderr
        # 検知結果を毎回リセットする — インスタンスが再利用されないのが
        # 通例でも、前回呼び出しの残骸を次の `run()` に持ち越さない。
        self._recovery_stderr_fatal = None

        cause, rc, stdout_lines, stderr_chunks = self._run_cli_process(
            inner_argv, env, timeout_sec=primary_timeout,
            on_started=self._cli_started_sink, abort_event=self._abort_event,
            prompt=stdin_prompt)

        # 段A: どの終端経路 (timeout/failed/schema mismatch/completed) でも
        # 漏れなく保存する — 追撃 (`_recover_output`) より前のこの位置に
        # 置く (追撃分は backend 側が別途自前で保存する、既存の流儀)。
        self._save_transcript(list(stdout_lines), stderr_chunks)

        # A4 10 回目 #71 観測 B (2026-09-11): どの終端経路でも (completed
        # を含む — codex の code-mode host 無力化はまさに rc=0/completed
        # で起きた) stderr に既知の致命パターンが無いか調べておく。
        stderr_fatal = _detect_stderr_fatal("".join(stderr_chunks))

        # M4: どちらの追撃経路 (timeout/no-output) を通って `raw` が
        # 得られたかを覚えておき、schema 検証を通った completed にだけ
        # `recovered=True` を立てる (provenance — improve_loop が report
        # artifact を observation へ降格する判断材料)。
        via_recovery = False

        if cause in ("timeout", "abort"):
            # 段B: timeout 経路でも追撃回収を 1 回試みる (SIGTERM 中断後も
            # session が継続できる backend 向け)。基底実装は no-op (None) の
            # ため、追撃を実装しない backend は従来どおり "timeout" になる。
            raw = self._recover_output(mission, list(stdout_lines), recovery_timeout)
            stderr_fatal = _merge_stderr_fatal(stderr_fatal, self._recovery_stderr_fatal)
            if cause == "abort":
                abort_reason = _normalize_reason(
                    self._abort_reason_fn() if self._abort_reason_fn
                    else TOOL_BUDGET_ABORT_PREFIX)
            if raw is None:
                if cause == "abort":
                    return MissionResult("failed", None, [], reason=abort_reason,
                                         stderr_fatal=stderr_fatal)
                return MissionResult("timeout", None, [], reason="cli timeout",
                                     stderr_fatal=stderr_fatal)
            via_recovery = True
        else:
            if rc != 0:
                stderr_text = "".join(stderr_chunks)
                reason = _normalize_reason(
                    f"HTTP-like CLI exit rc={rc}: {stderr_text.splitlines()[0] if stderr_text else ''}")
                return MissionResult("failed", None, [], reason=reason,
                                     stderr_fatal=stderr_fatal)

            raw = self._extract_output(stdout_lines, self._workdir)
            if raw is None:
                # 段B: 最終出力が回収できない場合も追撃回収を 1 回試みる。
                raw = self._recover_output(mission, list(stdout_lines), recovery_timeout)
                stderr_fatal = _merge_stderr_fatal(stderr_fatal, self._recovery_stderr_fatal)
                if raw is None:
                    return MissionResult("failed", None, [],
                                         reason=_normalize_reason("no output recovered from cli"),
                                         stderr_fatal=stderr_fatal)
                via_recovery = True
        try:
            jsonschema.validate(raw, mission.output_schema)
        except jsonschema.ValidationError as e:
            return MissionResult(
                "failed", None, [],
                reason=_normalize_reason(f"output_schema mismatch: {e.message}"),
                stderr_fatal=stderr_fatal)
        # /code-review 2 周目 #2 (2026-09-08): abort → 追撃で回収できた
        # completed は abort の provenance (reason) を保持する — worker の
        # summary 付与と親の activity がこれを読む。
        return MissionResult("completed", raw, [], recovered=via_recovery,
                             reason=abort_reason if cause == "abort" else None,
                             stderr_fatal=stderr_fatal)

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

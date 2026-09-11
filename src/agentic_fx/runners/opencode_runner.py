"""OpencodeRunner — 隔離した opencode CLI で improve を実行する。"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Literal

from agentic_fx._safe_error import safe_error_text
from agentic_fx.runners.base import Mission
from agentic_fx.runners.cli_runner import CliRunner, _detect_stderr_fatal
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry

#: 段B (probe 2026-08-30 実証): 一般モデル (qwen3.8/ornith) は opencode で
#: 「作業はするが最終テキストを出さずに終わる」ことがある。中断/timeout 後の
#: session (`-s <id>`) へこのプロンプトで 1 回だけ追撃し、KV cache 再利用で
#: schema 準拠 JSON を回収する。
_log = logging.getLogger("agentic_fx.runners.opencode")

_RESUME_PROMPT = (
    "これまでの作業を踏まえ、指定された JSON schema "
    "(discoveries / selected / artifact / selection_rationale) の最終出力だけを"
    "今すぐ出力してください。コードフェンスや説明文は不要です。実施が未完了の場合は"
    "artifact を observation 形にして reason に状況を書いてください。")

#: 追撃 1 回の上限 (秒)。probe 実測 (18 秒) に対し十分な余裕を持たせる。
#: mission 総予算の内数 (M2) のため、実際に使う値は
#: `min(_RESUME_TIMEOUT_SEC, recovery_timeout_sec)`。
_RESUME_TIMEOUT_SEC = 180

#: M2: mission 総予算のうち追撃用に取り分ける秒数。primary の timeout は
#: `mission.timeout_sec - _RESUME_RESERVE_SEC` に短縮される
#: (`mission.timeout_sec` がこれ以下なら reserve 自体を無効化する — 基底
#: `CliRunner.run()` 側の規約)。
_RESUME_RESERVE_SEC = 240.0

#: Minor5: opencode の session id 形式 (`ses_...`) の検証。不正な形式は
#: 追撃対象として扱わない (fail closed)。
_SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")


def _disable_mcp_for_recovery(workdir: Path) -> bool:
    """追撃 (session resume) の前に workdir の opencode 設定で afx MCP を
    無効化する (設計 v4 §Tier F — 追撃は最終 JSON の回収専用で tool を
    再実行させない。実 opencode 1.18.25 で resume が home config を再読込
    することを probe で確認済み: `tmp/design-mission-abort/probe-resume-mcp.md`)。

    設定が読めない / 形が違う場合は **無効化を諦めて追撃は続行** する
    (追撃は best-effort であり、ここで例外を出すと回収経路ごと失う)。
    戻り値は無効化できたか。"""
    path = workdir / "home/.config/opencode/opencode.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        config["mcp"]["afx"]["enabled"] = False
        path.write_text(json.dumps(config), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _log.warning("could not disable MCP for recovery: %s", safe_error_text(exc))
        return False
    return True


class OpencodeRunner(CliRunner):
    """improve 専用。CLI に上限指定がないため max_turns は無視する。"""
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 llama_swap_base_url: str, context_limit: int,
                 mcp_timeout_ms: int,
                 cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None,
                 abort_event: threading.Event | None = None,
                 abort_reason_fn: Callable[[], str] | None = None) -> None:
        self._llama_swap_base_url = llama_swap_base_url
        self._context_limit = context_limit
        self._mcp_timeout_ms = mcp_timeout_ms
        super().__init__(bin_path=bin_path, model=model, workdir=workdir,
                         cli_terminate_grace_sec=cli_terminate_grace_sec,
                         registry=registry, on_message=on_message,
                         cli_started_sink=cli_started_sink,
                         abort_event=abort_event,
                         abort_reason_fn=abort_reason_fn)

    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]:
        path = self._workdir / "home/.config/opencode/opencode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"provider": {"llama-swap": {
            "npm": "@ai-sdk/openai-compatible", "name": "llama-swap",
            "options": {"baseURL": self._llama_swap_base_url},
            "models": {self._model: {"name": self._model, "limit": {"context": self._context_limit, "output": 8192}}}}},
            "mcp": {"afx": {"type": "local", "command": [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(mcp_socket)], "enabled": True,
                            "timeout": self._mcp_timeout_ms}},
            # 組み込み tool は afx MCP 以外の全部を無効化 (実測: probe で bash が
            # tool_use イベント自体を出さなくなることを確認済み)。MCP 側は
            # "<server>_<tool>" (例: afx_list_staging) の別名前空間のため巻き込まれない。
            "tools": {"bash": False, "read": False, "write": False, "edit": False, "patch": False,
                      "glob": False, "grep": False, "list": False, "webfetch": False,
                      "websearch": False, "task": False, "todowrite": False, "question": False,
                      "skill": False}}), encoding="utf-8")
        return [str(self._bin_path), "run", mission.prompt, "--format", "json", "--pure", "-m", f"llama-swap/{self._model}", "--dir", str(self._workdir)]

    def _build_env(self, mission: Mission) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self._workdir / "home"), "TMPDIR": str(self._workdir / "tmp"), "TERM": "dumb", "PYTHONPATH": "", "PYTHONSAFEPATH": "1"}

    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict[str, Any] | None:
        from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA

        text = None
        for line in stdout_lines:
            try: event = json.loads(line)
            except (json.JSONDecodeError, ValueError): continue
            part = event.get("part")
            if event.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str): text = part["text"]
        if text is None: return None
        try: return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            try: return parse_json_output(
                text, prefer_keys=frozenset(IMPROVE_OUTPUT_SCHEMA["required"]))
            except ParseError: return None

    def _max_turns_semantics(self) -> Literal["passthrough", "ignored"]: return "ignored"

    @staticmethod
    def _extract_session_id(stdout_lines: list[str]) -> str | None:
        """イベント行の `sessionID` フィールド (`ses_...`) を末尾から探す
        (probe 実証: 各イベント行に載る)。Minor5: `ses_[A-Za-z0-9]+` 形式に
        fullmatch しないものは (壊れたイベント行・偽装等) 追撃対象として
        扱わない — fail closed。"""
        for line in reversed(stdout_lines):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            sid = event.get("sessionID")
            if isinstance(sid, str) and _SESSION_ID_RE.fullmatch(sid):
                return sid
        return None

    @staticmethod
    def _last_step_finish_reason(stdout_lines: list[str]) -> str | None:
        """最後の `step_finish` イベントの `part.reason` を返す。

        opencode の実イベントでは終了理由はトップレベルではなく
        `part` (`{"type": "step-finish", "reason": "stop", ...}`) に入る。
        該当イベントまたは文字列の `part.reason` が無ければ None。
        """
        for line in reversed(stdout_lines):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("type") == "step_finish":
                part = event.get("part")
                reason = part.get("reason") if isinstance(part, dict) else None
                return reason if isinstance(reason, str) else None
        return None

    def _recovery_reserve_sec(self) -> float:
        return _RESUME_RESERVE_SEC

    def _recover_output(self, mission: Mission, stdout_lines: list[str],
                         recovery_timeout_sec: float) -> dict[str, Any] | None:
        """timeout / no-output からの追撃回収 (段B)。1 mission につき
        `run()` は timeout か no-output のどちらか一方の経路でしかこの
        フックを呼ばないため、実装上も追撃は最大 1 回に収まる。

        M2: `recovery_timeout_sec` (`run()` が `_recovery_reserve_sec()`
        から算出した、追撃に使ってよい残り秒数) が 0 以下なら追撃自体を
        行わない (mission 総予算を超えて追撃しない)。追撃 timeout は
        `min(_RESUME_TIMEOUT_SEC, recovery_timeout_sec)`。

        M1: プロセス起動は主呼び出しと同じ `_run_cli_process` (launcher
        経由・`start_new_session=True`・`self._rlimits` 適用・timeout 時は
        pgid 単位の SIGTERM→grace→SIGKILL) を再利用する — 追撃だけ別系統の
        `Popen.communicate()`/`kill()` にしない。

        M3: `rc == 0` かつ最終 `step_finish` イベントの `reason == "stop"`
        の両方を必須にする (probe p3 実測: 正常な追撃は reason:"stop")。
        どちらか欠けたら None。
        破棄理由は transcript の先頭 marker に記録する。

        追撃自体の失敗 (rc≠0/timeout/reason 不一致) は握って None を返す —
        mission を悪化させない (追撃前と同じ failed/timeout に落ちるだけ)。
        追撃分の stdout イベント行は、`CliRunner._save_transcript` (run()
        の finally で既に呼ばれた後) とは別に、ここで自前保存する —
        `_recover_output` の実行は必ず finally より後 (このメソッド自体が
        finally 完了後の return 前でしか呼ばれない) のため、既存の
        transcript ファイルに追記する経路が無く、別ファイルに保存する形を
        取る。"""
        if recovery_timeout_sec <= 0:
            return None
        session_id = self._extract_session_id(stdout_lines)
        if session_id is None:
            return None
        _disable_mcp_for_recovery(self._workdir)
        argv = [str(self._bin_path), "run", _RESUME_PROMPT, "--format", "json",
                "--pure", "-m", f"llama-swap/{self._model}", "--dir", str(self._workdir),
                "-s", session_id]
        env = self._build_env(mission)
        resume_timeout = min(_RESUME_TIMEOUT_SEC, recovery_timeout_sec)
        cause, rc, resume_lines, stderr_chunks = self._run_cli_process(
            argv, env, timeout_sec=resume_timeout)
        # codex 1 周目 I4 是正 (2026-09-11): 追撃 (resume) プロセスの
        # stderr は primary とは別プロセスから来るため、`CliRunner.run()`
        # が primary の stderr だけを見る `_detect_stderr_fatal` では拾えない
        # — ここで検知して `self._recovery_stderr_fatal` に置き、`run()` が
        # primary 分と合成する (`_merge_stderr_fatal`)。
        self._recovery_stderr_fatal = _detect_stderr_fatal("".join(stderr_chunks))
        step_finish_reason = self._last_step_finish_reason(resume_lines)
        if cause == "timeout":
            discard_reason = "timed_out"
        elif rc != 0:
            discard_reason = "rc"
        elif step_finish_reason != "stop":
            discard_reason = "step_finish_reason"
        else:
            discard_reason = None
        accepted = discard_reason is None
        marker = json.dumps({
            "_resume_recovery": True,
            "session_id": session_id,
            "timed_out": cause == "timeout",
            "rc": rc,
            "step_finish_reason": step_finish_reason,
            "accepted": accepted,
            "discard_reason": discard_reason,
            "stderr_tail": "".join(stderr_chunks)[-2000:],
        })
        self._save_transcript([marker, *resume_lines], stderr_chunks)
        self._on_message({"type": "event", "message": {
            "role": "system", "content": marker}})
        if cause == "timeout" or rc != 0:
            return None
        if step_finish_reason != "stop":
            return None
        return self._extract_output(resume_lines, self._workdir)

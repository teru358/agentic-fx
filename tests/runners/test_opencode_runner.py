"""OpencodeRunner の隔離 CLI 契約 pin。"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from agentic_fx.runners.base import Mission
from agentic_fx.runners.opencode_runner import OpencodeRunner
from agentic_fx.tools.registry import ToolRegistry


def _mission() -> Mission:
    return Mission(prompt="reply", tools=[], output_schema={"type": "object"},
                   max_turns=1, timeout_sec=10)


def _runner(tmp_path):
    workdir = tmp_path / "wd"; workdir.mkdir()
    return OpencodeRunner(bin_path=Path("/opt/opencode"), model="qwen-test",
                          workdir=workdir,
                          llama_swap_base_url="http://localhost:8080/v1",
                          cli_terminate_grace_sec=1, registry=ToolRegistry()), workdir


# --- 段B: session resume による追撃回収 ---------------------------------
#
# `_recover_output` は実際に opencode CLI を再実行する — Popen をモックする
# と `CliRunner.run()` の `os.getpgid(proc.pid)`/`killpg` が実プロセスを
# 前提にしており fake proc では動かない (`test_cli_runner.py` の既存流儀と
# 同じ理由)。そのため「fake opencode」として実行可能な python スクリプトを
# 用意し、実 subprocess として起動する — argv に `-s <id>` が含まれるかで
# 初回呼び出しと追撃呼び出しを区別させる。

_RESUME_SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
                  "required": ["answer"]}


def _write_fake_opencode(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_opencode"
    script.write_text(f"#!{sys.executable}\n{body}")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _resume_mission(**over) -> Mission:
    # timeout_sec の既定は 300: OpencodeRunner の `_recovery_reserve_sec()`
    # (240) より大きくしておかないと、M2 (予算の内数化) により reserve が
    # 無効化されて追撃自体が起こらない (`_recover_output` が
    # `recovery_timeout_sec<=0` で即 None) — 追撃の中身 (session id /
    # rc / reason 検査) を検証したいテストはこの既定のままにする。
    d = dict(prompt="do it", tools=[], output_schema=_RESUME_SCHEMA,
             max_turns=1, timeout_sec=300)
    d.update(over)
    return Mission(**d)


def _resume_runner(tmp_path: Path, bin_path: Path, **over):
    workdir = tmp_path / "wd"; workdir.mkdir(exist_ok=True)
    kw = dict(bin_path=bin_path, model="qwen-test", workdir=workdir,
              llama_swap_base_url="http://localhost:8080/v1",
              cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    kw.update(over)
    return OpencodeRunner(**kw), workdir


def test_opencode_resumes_session_and_recovers_json_after_no_output(tmp_path):
    """(a) 初回が text イベント無しで終わっても、`sessionID` を使った追撃
    argv (`-s <id>`) が組まれ、追撃出力 (rc=0 かつ最終 step_finish の
    reason=="stop") の JSON が回収されて completed になる。"""
    body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    session_id = argv[argv.index('-s') + 1]\n"
        "    text = json.dumps({'answer': 4, 'resumed': session_id})\n"
        "    print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': text}}))\n"
        "    print(json.dumps({'type': 'step_finish', 'reason': 'stop'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_abc123'}))\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission())
    assert result.status == "completed"
    assert result.output == {"answer": 4, "resumed": "ses_abc123"}


def test_opencode_no_session_id_falls_back_to_failed_without_resume(tmp_path):
    """(b) sessionID がイベント行に無ければ追撃せず、従来どおり failed
    (`no output` reason) になる。"""
    body = "import json\nprint(json.dumps({'type': 'step_start'}))\n"
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason


def test_opencode_resume_without_text_also_fails(tmp_path):
    """(c) sessionID はあるが追撃出力にも text イベントが無ければ failed
    (`no output` reason) のまま。"""
    body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    print(json.dumps({'type': 'step_finish'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_xyz'}))\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason


def test_opencode_timeout_path_also_triggers_resume(tmp_path):
    """(d) timeout 終端 (CLI が sessionID 出力後にハングして
    `_terminate_pgid` で殺される) でも追撃が走り、回収できれば completed
    になる。M2 (予算の内数化) 対応: mission.timeout_sec (240.5) を
    reserve (240) より僅かに大きくし、primary の実 timeout を 0.5 秒に
    縮めてテストを高速に保ちつつ追撃予算 (240 秒、実際は即完了) を残す。"""
    body = (
        "import sys, json, time\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    text = json.dumps({'answer': 7})\n"
        "    print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': text}}))\n"
        "    print(json.dumps({'type': 'step_finish', 'reason': 'stop'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_timeout'}), flush=True)\n"
        "    time.sleep(600)\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission(timeout_sec=240.5))
    assert result.status == "completed"
    assert result.output == {"answer": 7}


# --- M1: 追撃のプロセス管理は primary と同じ規律を使う -------------------


def test_opencode_resume_uses_launcher_start_new_session_and_rlimits(tmp_path):
    """M1 (プロセス管理の統一) killer: 追撃 subprocess も primary と同じ
    `_run_cli_process` 経由 — launcher (`python -c <LAUNCHER_SOURCE> ...`)
    を通り、`start_new_session=True` かつ `self._rlimits` が JSON として
    launcher argv に乗る。primary 呼び出しを直接 `Popen(argv, kill=...)`
    のような別系統にする変異はここで壊れる。"""
    import subprocess as _subprocess

    body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    text = json.dumps({'answer': 4})\n"
        "    print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': text}}))\n"
        "    print(json.dumps({'type': 'step_finish', 'reason': 'stop'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_abc123'}))\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    want_rlimits = {"RLIMIT_FSIZE": (1024, 1024)}
    runner._rlimits = want_rlimits  # OpencodeRunner は rlimits を公開 kw に
    # していないため、CliRunner が既に持つ instance 属性を直接差し替える。

    captured: list[tuple[tuple, dict]] = []
    real_popen = _subprocess.Popen

    def spying_popen(*a, **kw):
        captured.append((a, kw))
        return real_popen(*a, **kw)

    runner._popen = spying_popen
    result = runner.run(_resume_mission())
    assert result.status == "completed"
    # primary + 追撃 の 2 回起動される。
    assert len(captured) == 2
    for args, kwargs in captured:
        assert kwargs.get("start_new_session") is True
        launcher_argv = args[0]
        assert launcher_argv[0] == sys.executable
        assert launcher_argv[1] == "-c"
        assert launcher_argv[4] == json.dumps(want_rlimits)


# --- M3: rc==0 かつ最終 step_finish の reason=="stop" を必須にする -------


def test_opencode_resume_marker_records_nonzero_rc_discard(tmp_path, monkeypatch):
    messages = []
    runner, workdir = _resume_runner(
        tmp_path, Path("/opt/opencode"), on_message=messages.append)
    resume_lines = [
        '{"type":"text","part":{"type":"text","text":"{\\"answer\\":4}"}}',
        '{"type":"step_finish","reason":"stop"}',
    ]
    saved = []
    monkeypatch.setattr(
        runner, "_run_cli_process",
        lambda *args, **kwargs: (False, 1, resume_lines, []))
    monkeypatch.setattr(runner, "_save_transcript", lambda stdout, stderr: saved.append(stdout))

    result = runner._recover_output(
        _resume_mission(), ['{"sessionID":"ses_rcmarker"}'], 10)

    assert result is None
    marker = json.loads(saved[0][0])
    assert marker["accepted"] is False
    assert marker["discard_reason"] == "rc"
    assert marker["rc"] == 1
    assert messages == [{"type": "event", "message": {
        "role": "system", "content": saved[0][0]}}]


def test_opencode_resume_marker_records_timeout_discard(tmp_path, monkeypatch):
    runner, _workdir = _resume_runner(tmp_path, Path("/opt/opencode"))
    resume_lines = ['{"type":"step_finish","reason":"stop"}']
    saved = []
    monkeypatch.setattr(
        runner, "_run_cli_process",
        lambda *args, **kwargs: (True, 0, resume_lines, []))
    monkeypatch.setattr(runner, "_save_transcript", lambda stdout, stderr: saved.append(stdout))

    result = runner._recover_output(
        _resume_mission(), ['{"sessionID":"ses_timeoutmarker"}'], 10)

    assert result is None
    marker = json.loads(saved[0][0])
    assert marker["accepted"] is False
    assert marker["discard_reason"] == "timed_out"


def test_opencode_resume_marker_records_finish_reason_discard(tmp_path, monkeypatch):
    runner, _workdir = _resume_runner(tmp_path, Path("/opt/opencode"))
    resume_lines = ['{"type":"step_finish","reason":"max_tokens"}']
    saved = []
    monkeypatch.setattr(
        runner, "_run_cli_process",
        lambda *args, **kwargs: (False, 0, resume_lines, []))
    monkeypatch.setattr(runner, "_save_transcript", lambda stdout, stderr: saved.append(stdout))

    result = runner._recover_output(
        _resume_mission(), ['{"sessionID":"ses_reasonmarker"}'], 10)

    assert result is None
    marker = json.loads(saved[0][0])
    assert marker["accepted"] is False
    assert marker["discard_reason"] == "step_finish_reason"


def test_opencode_resume_marker_records_accepted_recovery(tmp_path, monkeypatch):
    messages = []
    runner, _workdir = _resume_runner(
        tmp_path, Path("/opt/opencode"), on_message=messages.append)
    resume_lines = [
        '{"type":"text","part":{"type":"text","text":"{\\"answer\\":4}"}}',
        '{"type":"step_finish","reason":"stop"}',
    ]
    saved = []
    monkeypatch.setattr(
        runner, "_run_cli_process",
        lambda *args, **kwargs: (False, 0, resume_lines, []))
    monkeypatch.setattr(runner, "_save_transcript", lambda stdout, stderr: saved.append(stdout))

    result = runner._recover_output(
        _resume_mission(), ['{"sessionID":"ses_acceptedmarker"}'], 10)

    assert result == {"answer": 4}
    marker = json.loads(saved[0][0])
    assert marker["accepted"] is True
    assert marker["discard_reason"] is None
    assert messages == [{"type": "event", "message": {
        "role": "system", "content": saved[0][0]}}]


def test_opencode_resume_nonzero_rc_is_rejected(tmp_path):
    """M3 killer: 追撃 CLI が rc!=0 で終われば、text/step_finish が
    正しくても None (failed) にする。"""
    body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    text = json.dumps({'answer': 4})\n"
        "    print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': text}}))\n"
        "    print(json.dumps({'type': 'step_finish', 'reason': 'stop'}))\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_rc'}))\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason


def test_opencode_resume_reason_not_stop_is_rejected(tmp_path):
    """M3 killer: 最終 step_finish の reason が "stop" 以外 (例:
    "max_tokens" — 打ち切り) なら、text/rc が正しくても None (failed)
    にする。"""
    body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    text = json.dumps({'answer': 4})\n"
        "    print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': text}}))\n"
        "    print(json.dumps({'type': 'step_finish', 'reason': 'max_tokens'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_reason'}))\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason


def test_opencode_resume_missing_step_finish_is_rejected(tmp_path):
    """M3 killer (境界): step_finish イベント自体が無ければ
    `_last_step_finish_reason` は None — "stop" と一致せず None (failed)
    になる。"""
    body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if '-s' in argv:\n"
        "    text = json.dumps({'answer': 4})\n"
        "    print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': text}}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'step_start', 'sessionID': 'ses_nofinish'}))\n"
    )
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    result = runner.run(_resume_mission())
    assert result.status == "failed"


# --- Minor5: sessionID の形式検証 ---------------------------------------


def test_opencode_invalid_session_id_format_skips_resume(tmp_path):
    """Minor5 killer: `sessionID` が `ses_[A-Za-z0-9]+` に fullmatch しない
    (例: 空白混入・別プレフィックス) 場合、追撃 subprocess を起動しない —
    popen 呼び出し回数が primary の 1 回のみであることまで pin する。"""
    import subprocess as _subprocess

    body = "import json\nprint(json.dumps({'type': 'step_start', 'sessionID': 'not the right format!'}))\n"
    script = _write_fake_opencode(tmp_path, body)
    runner, _workdir = _resume_runner(tmp_path, script)
    captured: list[tuple] = []
    real_popen = _subprocess.Popen

    def spying_popen(*a, **kw):
        captured.append(a)
        return real_popen(*a, **kw)

    runner._popen = spying_popen
    result = runner.run(_resume_mission())
    assert result.status == "failed"
    assert result.reason is not None and "no output" in result.reason
    assert len(captured) == 1  # primary のみ、追撃は起動されない


def test_opencode_argv_writes_self_contained_mcp_provider_config(tmp_path):
    """mcp/provider 節を落とす変異では、隔離 HOME で tools も provider も
    見えず起動不能になるため、生成済み設定と argv の両方を pin する。
    `tools` 節 (組み込み tool 無効化) を落とす変異は、bash/read 等での
    project 外境界越え・徘徊 (probe 実証済み、2026-08-30) を再度許してしまう
    ため、無効化する tool の完全な集合も pin する。"""
    runner, workdir = _runner(tmp_path)
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    config = json.loads((workdir / "home/.config/opencode/opencode.json").read_text())
    assert argv == ["/opt/opencode", "run", "reply", "--format", "json", "--pure",
                    "-m", "llama-swap/qwen-test", "--dir", str(workdir)]
    assert config["provider"]["llama-swap"]["options"]["baseURL"] == "http://localhost:8080/v1"
    assert config["provider"]["llama-swap"]["models"]["qwen-test"]["limit"] == {"context": 131072, "output": 8192}
    assert config["mcp"]["afx"]["command"] == [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(workdir / "afx.sock")]
    assert config["tools"] == {
        "bash": False, "read": False, "write": False, "edit": False, "patch": False,
        "glob": False, "grep": False, "list": False, "webfetch": False,
        "websearch": False, "task": False, "todowrite": False, "question": False,
        "skill": False}


def test_opencode_env_is_minimal_and_output_is_last_text_json(tmp_path):
    """親 env 継承または最後の text 以外を採る変異を防ぐ。最後の assistant
    text が唯一の採用対象で、非 JSON は fail closed (None) にする。"""
    runner, workdir = _runner(tmp_path)
    assert runner._build_env(_mission()) == {
        "PATH": "/usr/bin:/bin", "HOME": str(workdir / "home"),
        "TMPDIR": str(workdir / "tmp"), "TERM": "dumb", "PYTHONPATH": "",
        "PYTHONSAFEPATH": "1"}
    assert runner._extract_output([
        '{"type":"text","part":{"type":"text","text":"{\\"old\\": 1}"}}',
        '{"type":"text","part":{"type":"text","text":"{\\"answer\\": 4}"}}'], workdir) == {"answer": 4}
    assert runner._extract_output(['{"type":"text","part":{"type":"text","text":"not json"}}'], workdir) is None
    assert runner._max_turns_semantics() == "ignored"


def test_opencode_extract_output_fails_closed_when_no_text_event(tmp_path):
    """検収変異 M3 (2026-08-30) killer: stdout に text イベントが 1 つも
    無いとき `_extract_output` は None (fail closed)。`{}` 等を返す変異は
    親の出力検証を素通りしかねない。"""
    from agentic_fx.runners.opencode_runner import OpencodeRunner
    from agentic_fx.tools.registry import ToolRegistry
    workdir = tmp_path / "wd"; workdir.mkdir()
    runner = OpencodeRunner(
        bin_path=Path("/usr/bin/true"), model="m", workdir=workdir,
        llama_swap_base_url="http://127.0.0.1:1/v1",
        cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    lines = ['{"type": "step_start", "part": {"type": "step-start"}}',
             'not-json', '{"type": "step_finish"}']
    assert runner._extract_output(lines, workdir) is None

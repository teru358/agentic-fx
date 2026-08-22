#!/usr/bin/env python3
"""claude CLI の契約テスト用フェイク。argv を JSON で `<workdir>/observed_argv.json`
へ記録し、`<workdir>/_fake_claude_behavior` ファイルで挙動を切り替える。

`--output-format stream-json --verbose` を模し、NDJSON (1 行 1 イベント) を
stdout に吐く。最終行 (`type: "result"`) に `structured_output` を含める
(probe 実測: `{"answer":4}` を回収した形を踏襲)。

_fake_claude_behavior ファイルが存在する場合、その内容が behavior 値になる。
存在しない場合は "success" が既定。

対応する behavior:
  "success"        既定。schema 適合の structured_output を返し rc=0
  "fenced_output"  ```json フェンス付きで返す (llama-swap 経路の不安定さの
                   代用 — claude では稀だが _extract_output の頑健性検査用)
  "schema_mismatch" schema 不適合の出力を返す
  "hang_then_ignore_sigterm" SIGTERM を無視して sleep し続ける
  "nonzero_exit"   rc=1 で終了、stderr にエラーメッセージ
  "max_turns_exceeded" stop_reason="max_turns" 相当のエラー行を返す
"""
import json
import os
import signal
import sys
import time
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    workdir = Path.cwd()

    # argv を JSON で記録
    (workdir / "observed_argv.json").write_text(json.dumps(argv))
    (workdir / "observed_env.json").write_text(
        json.dumps(dict(os.environ)))

    # behavior を _fake_claude_behavior ファイルから読む (存在しない場合は "success")
    behavior_file = workdir / "_fake_claude_behavior"
    if behavior_file.exists():
        behavior = behavior_file.read_text().strip()
    else:
        behavior = "success"

    if behavior == "hang_then_ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(600)
        return 0

    init_event = {
        "type": "system", "subtype": "init",
        "cwd": str(workdir), "model": "claude-haiku-4-5",
        "tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        "mcp_servers": [{"name": "afx", "status": "connected"}],
        "slash_commands": [],
    }
    print(json.dumps(init_event))

    assistant_event = {"type": "assistant",
                       "message": {"role": "assistant",
                                  "content": [{"type": "text",
                                              "text": "working..."}]}}
    print(json.dumps(assistant_event))

    if behavior == "nonzero_exit":
        sys.stderr.write("claude: internal error occurred\n")
        return 1

    if behavior == "max_turns_exceeded":
        result_event = {
            "type": "result", "subtype": "error_max_turns",
            "num_turns": 1, "stop_reason": "max_turns",
            "is_error": True,
        }
        print(json.dumps(result_event))
        return 0

    if behavior == "schema_mismatch":
        result_event = {"type": "result", "subtype": "success",
                        "num_turns": 3, "stop_reason": "tool_use",
                        "structured_output": {"wrong_key": 1}}
        print(json.dumps(result_event))
        return 0

    if behavior == "fenced_output":
        result_event = {"type": "result", "subtype": "success",
                        "num_turns": 3, "stop_reason": "tool_use",
                        "result": "```json\n{\"answer\": 4}\n```"}
        print(json.dumps(result_event))
        return 0

    result_event = {"type": "result", "subtype": "success",
                    "num_turns": 3, "stop_reason": "tool_use",
                    "structured_output": {"answer": 4}}
    print(json.dumps(result_event))
    return 0


if __name__ == "__main__":
    sys.exit(main())

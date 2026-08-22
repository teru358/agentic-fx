#!/usr/bin/env python3
"""codex CLI (`exec --json`) の契約テスト用フェイク。argv を記録し、
`-o <path>` へ schema 適合の JSON を書く。stdout には `--json` を模した
NDJSON イベント行を吐く (transcript 転送の契約テスト用、フィールド名は
公開ドキュメントに基づく推定 — 上の Task 3 申し送り参照)。

FAKE_CODEX_BEHAVIOR:
  "success"          既定。schema 適合の出力を -o に書く
  "fenced_output"     -o に ```json フェンス付きで書く (probe P1② の再現)
  "schema_mismatch"   -o に schema 不適合の JSON を書く
  "hang_then_ignore_sigterm"
  "nonzero_exit"
  "missing_output_file"  -o を書かずに rc=0 で終了 (異常終了系)
"""
import json
import os
import signal
import sys
import time
from pathlib import Path


def _arg_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


def main() -> int:
    argv = sys.argv[1:]
    behavior = os.environ.get("FAKE_CODEX_BEHAVIOR", "success")
    workdir = Path.cwd()
    (workdir / "observed_argv.json").write_text(json.dumps(argv))
    (workdir / "observed_env.json").write_text(json.dumps(dict(os.environ)))

    if behavior == "hang_then_ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(600)
        return 0

    print(json.dumps({"type": "item.started",
                      "item": {"type": "agent_message"}}))
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message",
                              "text": "working..."}}))

    if behavior == "nonzero_exit":
        sys.stderr.write("codex: fatal error\n")
        return 1

    if behavior == "missing_output_file":
        return 0

    out_path = _arg_value(argv, "-o")
    if out_path is None:
        sys.stderr.write("fake_codex: -o not provided\n")
        return 2

    if behavior == "schema_mismatch":
        Path(out_path).write_text(json.dumps({"wrong_key": 1}))
    elif behavior == "fenced_output":
        Path(out_path).write_text("```json\n{\"answer\": 4}\n```")
    else:
        Path(out_path).write_text(json.dumps({"answer": 4}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

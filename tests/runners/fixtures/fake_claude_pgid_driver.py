#!/usr/bin/env python3
"""`test_worker_runner_reaps_real_cli_pgid_via_mission_worker_wiring` 専用の
fake claude CLI (trade profile)。

設計書 §D (T-D, [mission-prompt-in-argv-readable-via-proc]) 是正後、
`ClaudeRunner._build_argv` はもう `mission.prompt` を argv に積まない
(`workdir/prompt.txt` 経由の stdin に回る) ため、argv の中身に依存しない
fake CLI が要る。加えて `improve` profile は Landlock 適用前に
`elf_interpreter()` で `claude.bin` が ELF (PT_INTERP 読み取り可能) で
あることを要求するため、shebang script はそこでは使えない — この pin は
`trade` profile (Landlock 無し、`test_trade_claude_real_process_completes_
via_factory_build_runner` と同じ前例) を使うことでシェバン script のまま
成立させる。

自身の pid と、PDEATHSIG を継がない孫プロセスの pid を workdir 直下の
固定ファイル名へ書く。孫の生存が `WorkerRunner._terminate_cli_pgid` の
pgid 単位回収 (B2) の観測対象になる。SIGTERM は自分・孫の両方が無視する
(`_terminate_cli_pgid` の SIGTERM→grace→SIGKILL のエスカレーションが
実際に発火することを確認するため)。argv/stdin の中身は一切読まない。
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    workdir = Path.cwd()
    (workdir / "fake_claude_pid").write_text(str(os.getpid()))

    grandchild_marker = workdir / "fake_claude_grandchild_pid"
    # `start_new_session=True` を渡さない — このプロセス (launcher が
    # 立てたセッションリーダ) と同じ pgid に孫を残す。孫自身も prctl を
    # 呼ばないため PDEATHSIG を継がない。
    subprocess.Popen([
        sys.executable, "-c",
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(grandchild_marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n",
    ])
    time.sleep(600)
    return 0


if __name__ == "__main__":
    sys.exit(main())

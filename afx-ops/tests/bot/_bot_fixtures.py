"""bot の結合試験の装置: 偽 API daemon (実 UDS) の起動と script、隔離済み bot の起動 spec。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT_DAEMON = HERE / "_bot_daemon.py"

GUILD = 123456789012345678
CHANNEL = 223456789012345678
USER_A = 523456789012345671
USER_B = 523456789012345672
OUTSIDER = 523456789012345673
ROLE = 623456789012345678
# bot token の形 (base64url の 3 区切り)。中身は偽 Discord の照合にだけ使う
TOKEN = "XzIzNDU2Nzg5MDEyMzQ1Njc4OTAx.YWJjZGVm.ZmFrZS1kaXNjb3JkLXRva2VuLWZvci10ZXN0cw"
DIGEST_7 = "7" * 64
DIGEST_8 = "8" * 64


class BotDaemon:
    """`_bot_daemon.py` を起こし、script を書き換えられるようにする。"""

    def __init__(self, socket_path: Path, data_dir: Path, work: Path) -> None:
        self.script = work / "bot-script.json"
        self.record = work / "bot-record.jsonl"
        self.conf: dict = {"events": [], "approvals": {}}
        self._write()
        self.proc = subprocess.Popen([sys.executable, "-I", str(BOT_DAEMON), str(socket_path),
                                      str(data_dir), str(self.script), str(self.record)],
                                     stdout=subprocess.PIPE, text=True)
        word, _pid = self.proc.stdout.readline().split()
        assert word == "ready"

    def _write(self) -> None:
        tmp = self.script.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.conf))
        tmp.replace(self.script)

    def set(self, **changes) -> None:
        self.conf.update(changes)
        self._write()

    def requests(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line]

    def decisions(self) -> list[dict]:
        return [r for r in self.requests() if r["method"] == "POST"
                and r["path"].startswith("/v1/approvals/")]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        self.proc.stdout.close()


def event(event_id: int, *, code: str = "job_state_changed", approval_id: int | None = None,
          state: str = "done") -> dict:
    if code == "approval_state_changed":
        return {"id": event_id, "ts": "2026-10-11T00:00:00+00:00", "code": code,
                "ref": f"approval:{approval_id}",
                "fields": {"state": state, "decision_code": "approve",
                           "decided_by": "operator"}}
    return {"id": event_id, "ts": "2026-10-11T00:00:00+00:00",
            "code": "job_state_changed", "ref": "job:" + "0123456789abcdef" * 2,
            "fields": {"job_kind": "ask", "state": "done", "result_code": "ok"}}


def wait_for(predicate, limit: float = 20.0, interval: float = 0.05):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached")

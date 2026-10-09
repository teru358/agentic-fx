"""本番の env 構築経路で 3 種の worker を起こし、各 worker の /proc/<pid>/environ を集める。

usage: _worker_env_child.py <work_dir> <out_json>
環境変数 AFX_LEAK_SENTINEL に鍵文字列を置いてから起動される (親 env に鍵が漏れた最悪の
場合でも、worker の env 構築が親 env を引き継がないことを観測するため)。
<out_json> には worker ごとの environ (bytes を latin-1 で文字列化) を書く。
"""
from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
REPO = Path(__file__).resolve().parents[2]

from agentic_fx.config import load_settings  # noqa: E402
from agentic_fx.plugin.gate_pytest import run_gate_pytest  # noqa: E402
from agentic_fx.plugin.loader import PluginMeta, content_hash  # noqa: E402
from agentic_fx.plugin.sandbox import PluginSession  # noqa: E402
from agentic_fx.runners.worker_runner import _mission_worker_env  # noqa: E402


def _environ(pid: int) -> str:
    # exec の直後は空で読めることがあるので、中身が出るまで待つ。
    end = time.monotonic() + 10
    while True:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        if raw or time.monotonic() > end:
            return raw.decode("latin-1")
        time.sleep(0.01)


def _plugin_worker(work: Path, settings) -> str:
    d = work / "plugins" / "envprobe"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text("def compute(df, params):\n    return {'x': 1.0}\n")
    (d / "config.yaml").write_text("kind: indicator\noutputs: [x]\n")
    meta = PluginMeta(name="envprobe", kind="indicator", path=d, params={}, timeframe=None,
                      pairs=(), max_bars=200, content_hash=content_hash(d), outputs=("x",))
    with PluginSession(meta, settings=settings.plugin) as session:
        return _environ(session.pid)


def _improve_worker(work: Path) -> str:
    # WorkerRunner と同じ起動形 (同じ argv・同じ env 構築関数)。handshake を待つ間に読む。
    workdir = work / "improve-workdir"
    workdir.mkdir()
    proc = subprocess.Popen([sys.executable, "-m", "agentic_fx.mission_worker"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=str(workdir),
                            env=_mission_worker_env("improve"), start_new_session=True)
    try:
        return _environ(proc.pid)
    finally:
        proc.stdin.close()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(30)
        proc.stdout.close()


def _gate_worker(work: Path, settings) -> str:
    d = work / "candidate"
    d.mkdir()
    (d / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (d / "config.yaml").write_text("kind: indicator\n")
    # gate worker の中から自分の env をそのまま出す (失敗の詳細として stdout に載る)。
    # gate worker の Landlock は /proc を許さないので os.environ を読む。
    (d / "test_plugin.py").write_text(
        "import os\n"
        "def test_dump_environ():\n"
        "    raw = '\\0'.join(f'{k}={v}' for k, v in os.environ.items()).encode()\n"
        "    raise AssertionError('ENVIRON<' + raw.hex() + '>ENVIRON')\n")
    tail = run_gate_pytest(d, settings=settings).stdout_tail
    match = re.search(r"ENVIRON<([0-9a-f]+)>ENVIRON", tail)
    assert match is not None, tail
    return bytes.fromhex(match.group(1)).decode("latin-1")


def main() -> int:
    work, out = Path(sys.argv[1]), Path(sys.argv[2])
    assert os.environ.get("AFX_LEAK_SENTINEL")
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    result = {"plugin_worker": _plugin_worker(work, settings),
              "improve_worker": _improve_worker(work),
              "gate_pytest_worker": _gate_worker(work, settings),
              "self": _environ(os.getpid())}
    out.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

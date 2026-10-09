"""``afx --daemon`` 相当の子 process (実 run_service、実 signal、実 listener)。

usage: _daemon_child.py <root> [--peer-probe <out>]

外部へ出る取得 (価格・ニュース・経済指標) だけを失敗させ、runner と embedding を
fake にする。それ以外は本番の run_service と同じ経路で起動・停止する。
``--peer-probe`` を渡すと、listener が開いた後に daemon 自身の子 process から
正しい鍵で whoami を送り、その応答を <out> に書く。
"""
from __future__ import annotations

import json
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import yfinance  # noqa: E402

from agentic_fx import service as service_mod  # noqa: E402
from agentic_fx.datafeed.econ_calendar import CalendarFetch  # noqa: E402
from agentic_fx.runners.fake_runner import FakeRunner  # noqa: E402
from tests.store.test_rag import FakeEmbedding  # noqa: E402

PEER = Path(__file__).with_name("_daemon_peer.py")


def _offline(*_args, **_kwargs):
    raise ConnectionError("network is disabled in this test daemon")


def _peer_probe(root: Path, out: Path) -> None:
    socket_path = root / "data" / "run" / "api.sock"
    end = time.monotonic() + 60
    while not socket_path.exists():
        if time.monotonic() > end:
            out.write_text(json.dumps({"error": "socket never appeared"}))
            return
        time.sleep(0.05)
    # daemon の直接の子として要求を送る (二重 fork はしない)。
    subprocess.run([sys.executable, str(PEER), str(root), str(out)], timeout=60,
                   stdin=subprocess.DEVNULL)


def main() -> int:
    root = Path(sys.argv[1])
    original_build = service_mod.build_app

    def build_app(root_, *, stop_event=None, **_kwargs):
        app = original_build(root_, runner=FakeRunner([]), embedding_fn=FakeEmbedding(),
                             stop_event=stop_event)
        app.scheduler.on_improve_tick = None
        return app

    if sys.argv[2:4] and sys.argv[2] == "--peer-probe":
        threading.Thread(target=_peer_probe, args=(root, Path(sys.argv[3])),
                         daemon=True).start()
    with patch("agentic_fx.datafeed.news_collector.fetch_feed", return_value=[]), \
         patch("agentic_fx.datafeed.news_collector.fetch_web", return_value=[]), \
         patch("agentic_fx.datafeed.econ_calendar.fetch_ff_calendar",
               return_value=CalendarFetch(events=[], dropped=0)), \
         patch.object(yfinance, "download", _offline), \
         patch.object(yfinance, "Ticker", _offline), \
         patch.object(service_mod, "build_app", build_app):
        return service_mod.run_service(root, daemon=True)


if __name__ == "__main__":
    raise SystemExit(main())

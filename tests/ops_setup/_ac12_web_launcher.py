"""AC-12 の試験用に ``afx-ops web`` を unit から起動する入口。

実 ``afx_ops`` の config 読込・隔離 (Landlock / seccomp)・``wait_ready``・READY=1・web 本体
をそのまま通す。実 ``afx_ops.cli.main`` と違う点は 2 つだけで、どちらも ``startup.run`` が試験用に
受け付ける引数による: (1) 使い捨て unit の名前は ``afx-ops@web.service`` でないため cgroup 名の
照合を外す、(2) ``wait_ready`` の上限だけを spec で短縮でき、待ちの間隔・上限・終了値を記録する。

usage: python <this> web   (spec は ``<this>.json``)

隔離後は state dir 以外に書けないので、待ちの記録と終了値は state dir に、stderr は隔離前に
開いた fd 2 (work 配下の file) に出る。
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

spec = json.loads(Path(__file__ + ".json").read_text())
work = Path(spec["work"])
# 隔離後に書ける場所は state dir だけ (機能の固定領域)。観測値はそこへ置く
state = Path(spec["xdg_state"]) / "agentic-fx" / "ops" / "web"
os.environ["XDG_CONFIG_HOME"] = spec["xdg_config"]
os.environ["XDG_STATE_HOME"] = spec["xdg_state"]
log = os.open(work / "ops_stderr", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(log, 2)
os.close(log)

from afx_ops import api_client, cli  # noqa: E402
from afx_ops.config import read_config  # noqa: E402
from afx_ops.isolation import startup  # noqa: E402

trace = {"started": time.time(), "sleeps": [], "default_interval": api_client.READINESS_INTERVAL,
         "default_limit": api_client.READINESS_LIMIT, "limit": spec["limit"]}


def _write_trace() -> None:
    (state / "ops_trace.json").write_text(json.dumps(trace))


def _sleep(seconds: float) -> None:
    trace["sleeps"].append(seconds)
    time.sleep(seconds)


def _wait(endpoint) -> None:
    try:
        api_client.wait_ready(endpoint, limit=spec["limit"], sleep=_sleep)
    except api_client.ApiError as error:
        trace.update(wait_done=time.time(), outcome=error.reason)
        _write_trace()
        raise
    trace.update(wait_done=time.time(), outcome="ok")
    _write_trace()


config = read_config("web")
plan = dataclasses.replace(
    startup.StartupPlan.for_feature("web", web_port=config.port, web_bind=config.web_bind,
                                    web_auth=config.auth),
    check_unit=False)
status = startup.run(plan, config, cli.RUNTIMES["web"], plan_check=lambda _plan: None,
                     wait=_wait, preload=cli.PRELOADS["web"])
(state / "ops_exit").write_text(str(status))
sys.exit(status)

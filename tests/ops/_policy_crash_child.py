"""policy remove の DELETE commit 後・file replace 前に自分を SIGKILL する子。

argv: root policy_id
"""
from __future__ import annotations

import os
import resource
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from agentic_fx.activity import ActivityLog  # noqa: E402
from agentic_fx.config import load_settings  # noqa: E402
from agentic_fx.ops.contracts import Principal  # noqa: E402
from agentic_fx.ops.service import OpsService  # noqa: E402
from agentic_fx.store import db  # noqa: E402
from agentic_fx.store.state import StateStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def main(root: Path, policy_id: int) -> None:
    conn = db.connect(root / "data" / "agentic.db")
    service = OpsService(
        conn, wall_clock=lambda: datetime.now(timezone.utc),
        policy_path=root / "policy" / "directives.md",
        state_store=StateStore(root / "data" / "app_state.json"),
        plugins_root=root / "plugins",
        settings=load_settings(REPO / "config" / "settings.yaml.example"),
        activity_log=ActivityLog(root / "logs" / "activity.log"))

    def die(_self):
        os.kill(os.getpid(), signal.SIGKILL)

    OpsService._regenerate_policy = die
    service.remove_policy(Principal.OPERATOR, "key-r", policy_id)
    os._exit(0)


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))

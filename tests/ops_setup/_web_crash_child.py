"""別 process で web の非対話 enable を走らせ、指定点で os._exit して process 死を起こす。

引数は JSON 1 個: root, point, config_file, secret_action, password_file (省略可)。
XDG_CONFIG_HOME と HOME は呼び出し側が env で渡す。
"""
from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

from agentic_fx.ops_setup.interactive import interactive_enable
from agentic_fx.ops_setup.web_setup import WebOptions
from web_support import ONE_CANDIDATE, ScriptIO, StubSetup

CRASH_STATUS = 70


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    spec = json.loads(sys.argv[1])

    def hook(name: str) -> None:
        if name == spec["point"]:
            # finally も flush も通らない死に方。
            os._exit(CRASH_STATUS)

    options = WebOptions(non_interactive=True, config_file=spec["config_file"],
                         secret_action=spec["secret_action"], password_file=spec.get("password_file"))
    result = interactive_enable(Path(spec["root"]), "web", io=ScriptIO(tty=False),
                                probe_runner=lambda _feature: (0, ""),
                                setup_factory=lambda _root: StubSetup(), options=options,
                                candidates=lambda: ONE_CANDIDATE, hook=hook)
    sys.exit(0 if result.rc == 0 else 1)


if __name__ == "__main__":
    main()

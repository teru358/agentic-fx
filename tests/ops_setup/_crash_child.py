"""別 process で enable を走らせ、指定点で os._exit して process 死を起こす。

引数は JSON 1 個: root, unit_dir, afx_unit, ops_prefix, ops_exec, feature, point。
HOME は呼び出し側が env で渡す (鍵 dir は HOME から導かれる)。
"""
from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

from agentic_fx.ops_setup.stages import OpsSetup
from agentic_fx.ops_setup.systemd import Systemd
from agentic_fx.ops_setup.units import UnitPaths

CRASH_STATUS = 70


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    spec = json.loads(sys.argv[1])
    paths = UnitPaths(unit_dir=Path(spec["unit_dir"]), afx_unit=spec["afx_unit"],
                      ops_prefix=spec["ops_prefix"], ops_exec=spec["ops_exec"])

    def hook(name: str) -> None:
        if name == spec["point"]:
            # finally も flush も通らない死に方。lock fd の解放は kernel に任せる。
            os._exit(CRASH_STATUS)

    OpsSetup(Path(spec["root"]), systemd=Systemd(), paths=paths, hook=hook,
             config_check=lambda _root, _feature: True,
             probe=lambda _exec, _feature: (0, "")).enable(spec["feature"])
    sys.exit(0)


if __name__ == "__main__":
    main()

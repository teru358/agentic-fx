"""試験用の起動 plan と、それを通す緩い plan 検査。

production の plan 型には試験用の印を持たせない。試験は ``for_feature`` の plan を
``dataclasses.replace`` で一時領域・一時 port へ向け、``startup.run(plan, body,
plan_check=check_feature_only)`` で固定境界の検査だけを差し替える。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from afx_ops.isolation import startup


def test_plan(feature: str, *, config_dir: Path | None = None,
              secret_dir: Path | None = None, secret_files: tuple[str, ...] | None = None,
              state_dir: Path | None = None, web_port: int | None = None,
              connect_ports: tuple[int, ...] | None = None, check_unit: bool = False,
              require_notify: bool = False) -> startup.StartupPlan:
    base = startup.StartupPlan.for_feature(feature, web_port=web_port)
    changes: dict[str, Any] = {"check_unit": check_unit, "require_notify": require_notify}
    if config_dir is not None:
        changes["config_dir"] = config_dir
    if secret_dir is not None:
        changes["secret_dir"] = secret_dir
    if secret_files is not None:
        changes["secret_files"] = tuple(secret_files)
    if state_dir is not None:
        changes["state_dir"] = state_dir
    if connect_ports is not None:
        changes["connect_ports"] = tuple(connect_ports)
    return dataclasses.replace(base, **changes)


test_plan.__test__ = False  # pytest に試験関数として集めさせない


def check_feature_only(plan: startup.StartupPlan) -> None:
    """機能名だけを確かめる (unit の外・一時領域で起動するため)。"""
    if plan.feature not in startup.PLANS:
        raise startup.StartupError("plan_invalid", "feature")

"""prepare/run/commit の相構造で TradeLoop/ReflectionCycle が共有する
missions.finish の CAS 化された呼び出しヘルパー (プラン8, 設計書 §3.1 —
「独自実装だった _run_recorded 相当を共通化する」)。
"""
from __future__ import annotations

import logging
import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions

_log = logging.getLogger("agentic_fx.loops.mission_finalize")


def finalize_mission(conn: sqlite3.Connection, activity: ActivityLog,
                     clock: Clock, mid: int, result: MissionResult) -> bool:
    """`missions.finish` の CAS 化された呼び出し (**呼び出し元が
    core_lock を保持している前提**)。設計書 §4.7 codex C-4: 二重終端は
    上書きせず警告のみ残す。

    戻り値: `True` = 書込み試行が例外を出さなかった (CAS 受理・拒否
    いずれも)。`False` = 書込み自体 (`missions.finish`) が例外で失敗。

    **`False` (書込み失敗) をどう扱うかは呼び出し元の責務** — 本関数は
    それを規定しない (TradeLoop と ReflectionCycle で扱いが異なるため
    「fail closed」と一律には書けない):
    - `ReflectionCycle._reflect_one`: `False` を fail closed として扱い、
      reflection を保存しない (監査未確定の mission に紐づく reflection
      を残さないため — Task 16)。
    - `TradeLoop._run_once_impl`/`_ask_once_impl`: `False` でも直前の
      執行結果 (発注等) は巻き戻さない — 呼び出し元のコメント参照
      (Task 15 節「⚠ 着手前検証の結果 (4)」で明示的に許容された挙動)。
    """
    try:
        finished = missions.finish(conn, mid, result.status, result.output,
                                   result.transcript, clock.now())
    except Exception:  # noqa: BLE001
        _log.exception("missions.finish failed for %s", mid)
        try:
            activity.write(Category.SYSTEM, "mission_finalize_failed",
                           f"mid={mid}")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission_finalize_failed")
        return False
    if not finished:
        try:
            activity.write(Category.SYSTEM, "mission_finalize_conflict",
                           f"mid={mid} (already finalized elsewhere)")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission_finalize_conflict")
    return True

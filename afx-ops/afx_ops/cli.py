"""機能別の起動口: ``afx-ops <feature>``。

既知の機能名は必ず隔離の起動順 (`startup.run` と機能ごとに固定の plan) を通る。unit の
外で起動すると鍵を読む前に固定 reason で止まる。機能の中身が無い間は、隔離と自己検査が
通った後に固定 reason で非零終了する (何もせず成功したように見せない)。
"""
from __future__ import annotations

import sys
from collections.abc import Sequence

from afx_ops.isolation import startup

FEATURES = ("bot", "web", "notify")

RC_UNKNOWN_FEATURE = 2
RC_NOT_IMPLEMENTED = 69

REASON_UNKNOWN_FEATURE = "unknown_feature"
REASON_NOT_IMPLEMENTED = "feature_not_implemented"


def not_implemented(isolated: startup.Isolated | None) -> int:
    feature = isolated.feature if isolated is not None else "?"
    print(f"afx-ops: {REASON_NOT_IMPLEMENTED} ({feature})", file=sys.stderr, flush=True)
    return RC_NOT_IMPLEMENTED


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in FEATURES:
        print(f"afx-ops: {REASON_UNKNOWN_FEATURE} (usage: afx-ops {{{'|'.join(FEATURES)}}})",
              file=sys.stderr)
        return RC_UNKNOWN_FEATURE
    try:
        plan = startup.StartupPlan.for_feature(args[0])
    except Exception:  # plan の組立て (HOME の解決等) の失敗も traceback にしない
        print("afx-ops: startup_internal_error", file=sys.stderr, flush=True)
        return startup.RC_ISOLATION_FAILED
    return startup.run(plan, not_implemented)

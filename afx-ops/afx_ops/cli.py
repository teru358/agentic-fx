"""機能別の起動口: ``afx-ops <feature>``。

機能の中身が無い間は固定 reason で非零終了する。何もせず成功したように見えないこと、
鍵 dir・socket に触れないことが目的で、ここでは機能名の判定以外をしない。
"""
from __future__ import annotations

import sys
from collections.abc import Sequence

FEATURES = ("bot", "web", "notify")

RC_UNKNOWN_FEATURE = 2
RC_NOT_IMPLEMENTED = 69

REASON_UNKNOWN_FEATURE = "unknown_feature"
REASON_NOT_IMPLEMENTED = "feature_not_implemented"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in FEATURES:
        print(f"afx-ops: {REASON_UNKNOWN_FEATURE} (usage: afx-ops {{{'|'.join(FEATURES)}}})",
              file=sys.stderr)
        return RC_UNKNOWN_FEATURE
    print(f"afx-ops: {REASON_NOT_IMPLEMENTED} ({args[0]})", file=sys.stderr)
    return RC_NOT_IMPLEMENTED

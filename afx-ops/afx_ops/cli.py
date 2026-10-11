"""機能別の起動口: ``afx-ops <feature>``。

既知の機能名は必ず隔離の起動順 (`startup.run` と機能ごとに固定の plan) を通る。unit の
外で起動すると鍵を読む前に固定 reason で止まる。機能の中身が無い間は、隔離と自己検査が
通った後に固定 reason で非零終了する (何もせず成功したように見せない)。
"""
from __future__ import annotations

import sys
import os
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType

from afx_ops.config import ConfigError, read_config
from afx_ops.isolation import startup

FEATURES = ("bot", "web", "notify")

RC_UNKNOWN_FEATURE = 2
RC_NOT_IMPLEMENTED = 69

REASON_UNKNOWN_FEATURE = "unknown_feature"
REASON_NOT_IMPLEMENTED = "feature_not_implemented"


def feature_not_implemented(isolated: startup.Isolated) -> int:
    print(f"afx-ops: {REASON_NOT_IMPLEMENTED} ({isolated.feature})", file=sys.stderr, flush=True)
    return RC_NOT_IMPLEMENTED


RUNTIMES: Mapping[str, Callable[[startup.Isolated], int]] = MappingProxyType({
    "bot": feature_not_implemented,
    "web": feature_not_implemented,
    "notify": feature_not_implemented,
})


def _preload_bot() -> None:
    import discord  # noqa: F401


def _preload_web() -> None:
    import aiohttp.web  # noqa: F401


def _preload_notify() -> None:
    import aiohttp  # noqa: F401


PRELOADS: Mapping[str, Callable[[], None]] = MappingProxyType({
    "bot": _preload_bot,
    "web": _preload_web,
    "notify": _preload_notify,
})


def _close_unstarted_config(config) -> None:
    if config is not None and config.config_dir_fd is not None:
        os.close(config.config_dir_fd)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "probe" and args[1] in FEATURES:
        # 疎通確認は隔離しない短命 process (本体が subprocess で呼ぶ)。業務の起動順は通らない
        from afx_ops import probe
        return probe.main(args[1])
    if len(args) != 1 or args[0] not in FEATURES:
        print(f"afx-ops: {REASON_UNKNOWN_FEATURE} (usage: afx-ops {{{'|'.join(FEATURES)}}} | afx-ops probe {{{'|'.join(FEATURES)}}})",
              file=sys.stderr)
        return RC_UNKNOWN_FEATURE
    config = None
    try:
        config = read_config(args[0])
        plan = startup.StartupPlan.for_feature(args[0],
                                               web_port=config.port if args[0] == "web" else None)
    except ConfigError as exc:
        _close_unstarted_config(config)
        print(f"afx-ops: {exc.reason} ({exc.detail})", file=sys.stderr, flush=True)
        return startup.RC_ISOLATION_FAILED
    except Exception:  # plan の組立て (HOME の解決等) の失敗も traceback にしない
        _close_unstarted_config(config)
        print("afx-ops: startup_internal_error", file=sys.stderr, flush=True)
        return startup.RC_ISOLATION_FAILED
    return startup.run(plan, config, RUNTIMES[args[0]], preload=PRELOADS[args[0]])

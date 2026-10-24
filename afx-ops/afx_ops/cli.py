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

from afx_ops import bot as bot_runtime
from afx_ops.config import ConfigError, read_config
from afx_ops.isolation import startup
from afx_ops.notify import runtime as notify_runtime
from afx_ops.web import runtime as web_runtime

FEATURES = ("bot", "web", "notify")

RC_UNKNOWN_FEATURE = 2
RC_NOT_IMPLEMENTED = 69

REASON_UNKNOWN_FEATURE = "unknown_feature"
REASON_NOT_IMPLEMENTED = "feature_not_implemented"


def feature_not_implemented(isolated: startup.Isolated) -> int:
    print(f"afx-ops: {REASON_NOT_IMPLEMENTED} ({isolated.feature})", file=sys.stderr, flush=True)
    return RC_NOT_IMPLEMENTED


RUNTIMES: Mapping[str, Callable[[startup.Isolated], int]] = MappingProxyType({
    "bot": bot_runtime.run,
    "web": web_runtime.run,
    "notify": notify_runtime.run,
})


def _preload_bot() -> None:
    import discord  # noqa: F401

    # 業務処理の module も隔離前に読む (隔離後に新しい module を読み込まない)
    import afx_ops.bot.runtime  # noqa: F401


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


def _interfaces() -> int:
    """本機の up 状態の interface が持つ global / link の IPv4 を JSON 1 個で返す。

    config / state / secret の領域は読まず書かない。失敗は rc 71 と固定 reason の 1 行だけ。
    """
    from afx_ops import interfaces
    try:
        text = interfaces.render_json(interfaces.list_interfaces())
    except Exception:  # 想定外の失敗も列挙失敗と同じ固定出力にする
        print(f"afx-ops: {interfaces.REASON_LOOKUP_FAILED}", file=sys.stderr, flush=True)
        return startup.RC_ISOLATION_FAILED
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "probe" and args[1] in FEATURES:
        # 疎通確認は隔離しない短命 process (本体が subprocess で呼ぶ)。業務の起動順は通らない
        from afx_ops import probe
        return probe.main(args[1])
    if args == ["interfaces"]:
        return _interfaces()
    if len(args) != 1 or args[0] not in FEATURES:
        print(f"afx-ops: {REASON_UNKNOWN_FEATURE} (usage: afx-ops {{{'|'.join(FEATURES)}}} | afx-ops probe {{{'|'.join(FEATURES)}}} | afx-ops interfaces)",
              file=sys.stderr)
        return RC_UNKNOWN_FEATURE
    config = None
    try:
        config = read_config(args[0])
        web = args[0] == "web"
        plan = startup.StartupPlan.for_feature(
            args[0], web_port=config.port if web else None,
            web_bind=config.web_bind if web else startup.LOOPBACK, web_auth=config.auth)
    except ConfigError as exc:
        _close_unstarted_config(config)
        # web の reason は固定語だけを返し、設定の key 名も後ろに付けない
        suffix = "" if args[0] == "web" else f" ({exc.detail})"
        print(f"afx-ops: {exc.reason}{suffix}", file=sys.stderr, flush=True)
        return startup.RC_ISOLATION_FAILED
    except Exception:  # plan の組立て (HOME の解決等) の失敗も traceback にしない
        _close_unstarted_config(config)
        print("afx-ops: startup_internal_error", file=sys.stderr, flush=True)
        return startup.RC_ISOLATION_FAILED
    return startup.run(plan, config, RUNTIMES[args[0]], preload=PRELOADS[args[0]])

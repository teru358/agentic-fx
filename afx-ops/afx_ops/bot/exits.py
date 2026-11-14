"""bot unit の終了コードと、bot の失敗理由 (固定語) との対応。

値は afx-ops の他の入口 (0 / 1 / 2 / 3 / 4 / 69〜74) と重ならない。恒久は systemd の
`RestartPreventExitStatus` に入れて再起動を止め、一時は backoff で再起動させる。同じ数値でも
他の process (probe など) では別の意味を持つので、この表は bot unit の入口に限る。
"""
from __future__ import annotations

RC_OK = 0
# 恒久: 入力・state・配送先の拒否など、再起動しても同じ結果になる失敗
RC_BOT_PERMANENT = 78
# 一時: process 内の監視 (10 分) の後に backoff 再起動で回復し得る失敗
RC_BOT_TRANSIENT = 79
# event loop が 10 分止まった (watchdog が出す。発生源は loop の外にある)
RC_BOT_LOOP_STALLED = 80

PERMANENT_REASONS = frozenset({
    "token_invalid", "discord_login_failed", "bot_state_invalid", "payload_invalid",
    "events_invalid", "high_watermark_regressed", "discord_rejected",
})
TRANSIENT_REASONS = frozenset({
    "bot_state_unavailable", "discord_unavailable", "gateway_failed", "gateway_unhealthy",
    "delivery_stalled", "poller_stalled", "bot_internal_error",
})
REASONS = tuple(sorted(PERMANENT_REASONS | TRANSIENT_REASONS))


def rc_for_reason(reason: object) -> int:
    """bot の失敗理由から終了コードを決める。表に無い理由は一時 (再起動対象) にする。"""
    if reason in PERMANENT_REASONS:
        return RC_BOT_PERMANENT
    return RC_BOT_TRANSIENT

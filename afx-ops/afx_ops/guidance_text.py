"""status の「停止理由」と「次の操作」の固定文。

この module は `agentic_fx` の他の module を import しない。afx-ops へは byte 一致の写しとして
置く (正本はこの file。直接編集するのは正本だけ)。入力は daemon の status の `data` で、
新しい key が無い応答 (旧 daemon) では None を返し、呼び出し側が既存の state 表示に戻す。
不明・型違い・語彙の外は、ready や操作不要には倒さず「状態を確認」の案内にする。
"""
from __future__ import annotations

PHASES = ("unknown", "resume_requested", "ready", "resume_rejected", "awaiting_resume",
          "stopped", "restricted")
NEXT_ACTIONS = ("check_status", "wait_tick", "none", "restore_source", "resume_with_ack",
                "resume")
# daemon が返す (phase, next_action) の組。これ以外は不明として扱う
PAIRS = frozenset({
    ("unknown", "check_status"),
    ("resume_requested", "wait_tick"),
    ("ready", "none"),
    ("resume_rejected", "restore_source"),
    ("resume_rejected", "resume_with_ack"),
    ("resume_rejected", "resume"),
    ("awaiting_resume", "resume_with_ack"),
    ("awaiting_resume", "resume"),
    ("stopped", "restore_source"),
    ("restricted", "wait_tick"),
})
REJECTED_RESULTS = ("rejected_watermark", "rejected_unprocessed")
UNKNOWN_COUNT = "unknown"

RESUME = "`afx ctl --as approver data resume`"
RESUME_ACK = "`afx ctl --as approver data resume --acknowledge`"

_REASON = {
    "unknown": "状態を確認できません",
    "resume_requested": "再開要求を記録済みで、判定を待っています",
    "ready": "なし (データによる停止はありません)",
    "awaiting_resume": "取得の回復を確認済みで、人の再開操作を待っています",
    "stopped": "データの欠落を検知して停止しています",
    "restricted": "データの欠落を検知し、制限状態で自動復帰を判定しています",
}
_REJECTED_REASON = {
    "rejected_watermark": "直近の再開要求は、取得状態が確認できず拒否されました",
    "rejected_unprocessed": ("直近の再開要求は、未処理の足がある建玉の確認 "
                             "(--acknowledge) が無く拒否されました"),
}
_REJECTED_FALLBACK = "直近の再開要求は拒否されました"

_WAIT = "次の開場中の tick の判定待ちです。人の操作は要りません"
_RECHECK = "待ちの記録は発生時点のものです。取得状態を再確認してください"
_ACTION = {
    "check_status": "状態を確認できません。時間をおいて `afx ctl status` を再実行してください",
    "wait_tick": _WAIT,
    "none": "不要です",
    "restore_source": ("取得状態を確認し、回復後に再要求 (" + RESUME + ") してください。"
                       "判定は次の開場中の tick です"),
    "resume": RESUME + " を実行してください。判定は次の開場中の tick です",
    "resume_with_ack": ("未処理の足がある建玉を確認し、再開を承認するときだけ " + RESUME_ACK
                        + " を実行してください。判定は次の開場中の tick です"),
}
_RESUME_NO_ACK_NEEDED = ("未処理の足がある建玉は現在ありません。--acknowledge なしで再要求できます ("
                         + RESUME + ")。判定は次の開場中の tick です")


def checked_pair(feed: dict) -> tuple[str, str]:
    """phase と next_action。published の組でなければ不明 (ready や操作不要に倒さない)。

    ack の要否を決める行 (再開待ち・拒否後の再要求) は、現在の件数が非負の整数のときだけ
    その件数から決め直す。件数が読めなければ不明、正の数は ack つき、整数 0 だけ ack なし。
    """
    phase, action = feed.get("phase"), feed.get("next_action")
    if not (type(phase) is str and type(action) is str and (phase, action) in PAIRS):
        return "unknown", "check_status"
    if phase in ("awaiting_resume", "resume_rejected") and action in ("resume", "resume_with_ack"):
        count = count_of(feed)
        if count is None:
            return "unknown", "check_status"
        return phase, "resume_with_ack" if count else "resume"
    return phase, action


def count_of(feed: dict) -> int | None:
    """現在の未処理建玉数。非負の整数以外 (欠損・unknown・型違い) は None。"""
    value = feed.get("unprocessed_positions")
    if type(value) is int and value >= 0:
        return value
    return None


def guidance_lines(feed: object) -> tuple[str, str] | None:
    """(停止理由の行, 次の操作の行)。旧 API の応答 (案内の key が無い) では None。"""
    if type(feed) is not dict or "phase" not in feed and "next_action" not in feed:
        return None
    phase, action = checked_pair(feed)
    result = feed.get("resume_last_result")
    if phase == "resume_rejected":
        reason = _REJECTED_REASON.get(result, _REJECTED_FALLBACK) if type(result) is str else (
            _REJECTED_FALLBACK)
    else:
        reason = _REASON[phase]
    if phase in ("awaiting_resume", "resume_rejected"):
        count = count_of(feed)
        reason += " (未処理の足がある建玉 " + (str(count) if count is not None else "不明") + " 件)"
    if phase == "resume_rejected" and action == "resume" and result == "rejected_unprocessed":
        text = _RESUME_NO_ACK_NEEDED
    else:
        text = _ACTION[action]
    if phase in ("awaiting_resume", "resume_rejected"):
        text += "。" + _RECHECK
    return "停止理由: " + reason, "次の操作: " + text

"""plugin 画面の状態語を、人が読む日本語に写す表 (web が使う)。

語の正本は daemon の plugin read model (`agentic_fx/plugin/read_model.py` と `resolve.py`)。
afx-ops は本体を import しないので写しを持つ。表にない語は「不明 (<語>)」と出し、
どの語も「一致」や「配備中」には倒さない。
"""
from __future__ import annotations

import re

UNKNOWN = "不明"

KIND = {"strategy": "strategy", "indicator": "indicator", "signal": "signal", "unknown": UNKNOWN}

ENTITY = {"symlink": "配備中 (版を指す)", "plain": "配備中 (直置き)", "absent": "配備なし",
          "unreadable": "読めない"}
ENTITY_REASON = {
    "missing_files": "必要なファイルが無い", "invalid_manifest": "config.yaml が不正",
    "invalid_symlink": "symlink が不正", "version_mismatch": "版の hash が合わない",
    "io_error": "読み込みに失敗", "non_canonical_name": "名前が規則に合わない",
}

ADMISSION = {"loaded": "読込可", "excluded": "読込除外", "unapproved": "未承認",
             "unknown": UNKNOWN, "none": "-"}
ADMISSION_REASON = {"indicator_unresolved": "indicator を解決できない",
                    "never_approved": "承認の記録なし", "content_changed": "承認後に内容が変わった"}

DEPENDENCY = {
    "match": "一致", "not_found": "見つからない", "not_indicator": "indicator ではない",
    "unpinned": "pin なし", "pin_mismatch": "pin 不一致",
    "outputs_undeclared": "出力の宣言なし", "over_max_bars_limit": "max_bars が上限超過",
    "params_not_json_safe": "params が不正",
    "reference_timeframe_invalid": "参照の時間足が不正", "symbol_not_watched": "監視外の銘柄",
    "watch_symbol_too_long": "銘柄名が長すぎる", "max_age_invalid": "max_age が不正",
    "max_age_too_large": "max_age が大きすぎる",
    "reference_window_exceeds_max_bars": "参照の窓が max_bars を超える",
    "handshake_too_large": "handshake が大きすぎる", "family_key_too_long": "family key が長すぎる",
    "unknown": UNKNOWN,
}

ISSUE = {
    "duplicate_live": "配備が重複", "duplicate_pending": "承認待ちが重複",
    "duplicate_open_op": "途中の操作が重複", "kind_missing": "承認待ちに kind なし",
    "kind_mismatch": "承認待ちの kind が配備と違う",
    **{f"broken:{reason}": f"読めない ({text})" for reason, text in ENTITY_REASON.items()},
}

APPROVAL_STATUS = {"pending": "承認待ち", "approved": "承認済み", "rejected": "却下",
                   "expired": "期限切れ", "invalidated": "無効化"}
ORIGIN = {"staging": "改善の提出", "human": "人の提出", "reevaluation": "再評価"}
OPEN_OP_SOURCE = {"deploy": "配備操作", "switch": "切替"}
OPEN_OP_KIND = {"disable": "停止", "rollback": "巻き戻し", "retire_plain": "直置きの退避",
                "revoke": "承認の取り消し", "approve": "承認", "bless": "bless"}
SOURCE_STATE = {"ok": "読めた", "missing": "なし", "unavailable": "読めない"}

# 指標の棚卸し (usage_review)。語の正本は daemon の plugin/usage_review.py
USAGE_STATE = {"used": "使用中", "unused": "未使用", "indeterminate": "判定不能"}
USAGE_RESULT = {"holds": "当てはまる", "clear": "当てはまらない", "unknown": "判定不能"}
USAGE_ORIGIN = {"staging": "改善の提出", "human": "人の提出", "unknown": "出所不明"}
USAGE_NOT_PROPOSABLE = {"human_origin": "人が提出した版なので、停止の提案の対象外",
                        "unknown_origin": "出所を確認できない版なので、停止の提案の対象外"}
USAGE_REASON = {
    "scan_failed": "配備の走査を読み切れなかった",
    "pending_source_unreadable": "承認待ちを読めなかった",
    "pending_request_unreadable": "依存を読めない承認待ちがある",
    "candidates_unreadable": "戦略候補の記録を読めなかった",
    "key_mentioned": "読めない候補の key にこの名前が出てくる",
    "time_unreadable": "時刻を読めなかった",
    "journal_unreadable": "切替の記録を読めなかった",
    "no_generation_record": "配備した記録が見つからない",
    "generation_mismatch": "最後の配備記録がいまの版を指していない",
    "strategy_unreadable": "読めない配備中の entry があり、strategy の参照を確かめきれない",
}
# 条件ごとの文言 (k は未使用と見なすまでの週数)。当てはまる / 当てはまらない
USAGE_CONDITION = {
    "live_strategy": ("配備中の strategy が参照している", "参照する配備中の strategy はない"),
    "pending_approval": ("承認待ちの依頼が参照している", "参照する承認待ちはない"),
    "recent_candidate": ("直近 {k} 週の戦略候補に登場する", "直近 {k} 週の戦略候補には登場しない"),
    "recent_deploy": ("配備から {k} 週たっていない (試行期間中)", "配備から {k} 週以上たっている"),
}
USAGE_CONDITION_NAME = {"live_strategy": "配備中の strategy", "pending_approval": "承認待ち",
                        "recent_candidate": "戦略候補", "recent_deploy": "配備時期"}
USAGE_INVENTORY_REASON = {
    "scan_failed": "配備の走査を読み切れなかったため、参照の有無を確かめきれません",
    "journal_unreadable": "切替の記録を読めなかったため、配備時期を確かめきれません",
}
USAGE_EXCLUDED = {"null_key": "key なし", "unreadable_key": "key を読めない",
                  "unreadable_time": "作成時刻を読めない"}

_WORD = re.compile(r"[a-z][a-z0-9_:]{0,39}\Z")


def label(table: dict[str, str], word: object) -> str:
    """表にある語は日本語に、ない語は「不明 (<語>)」にする。語が固定語の形でなければ「不明」だけ。"""
    if type(word) is str and word in table:
        return table[word]
    if type(word) is str and _WORD.match(word):
        return f"{UNKNOWN} ({word})"
    return UNKNOWN

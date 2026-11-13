"""承認が履歴証跡で止まったときの固定 reason と文言 (web と承認カードが共有する)。

文言の正本は daemon 側 (`agentic_fx/plugin/history_reasons.py`)。afx-ops は本体を import
しないので写しを持つ。両 package の文言一致は tests/ops の 2 package 検査が pin する。
"""
from __future__ import annotations

EVIDENCE_INVALIDATED = "history_evidence_invalidated"
EVIDENCE_REFRESH_REQUIRED = "history_evidence_refresh_required"
CHECK_UNAVAILABLE = "history_check_unavailable"

TEXT = {
    EVIDENCE_INVALIDATED: (
        "評価証跡が現在の方針に適合しないため、依頼を失効しました。"
        "候補を `plugins/_human/<name>` に保存し、履歴条件を確認して人間submitし直してください"),
    EVIDENCE_REFRESH_REQUIRED: (
        "再評価証跡の更新が必要です。依頼は保留中です。履歴条件を確認し、"
        "`afx plugin reevaluate --all-deployed --yes` を再実行してください。"
        "整理のために却下しないでください"),
    CHECK_UNAVAILABLE: (
        "評価証跡を照合できません。依頼は保留中です。"
        "`afx status` で設定・読取エラーを確認し、修復後に承認を再試行してください"),
}


def text_for(reason: object) -> str | None:
    """固定 reason の文言。知らない reason・型違いは None (何も足さない)。"""
    return TEXT.get(reason) if type(reason) is str else None

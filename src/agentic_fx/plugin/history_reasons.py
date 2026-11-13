"""承認が履歴証跡で止まったときの固定 reason と、人間向けの文言 (全表示面が共有する)。

文言の正本はここ。web と承認カードは別 package なので同じ文言の写しを持ち、
tests/ops の 2 package 検査が一致を pin する。agent へは渡さない。
"""
from __future__ import annotations

OUTCOME = "history_not_ready"

# 通常依頼の確定した証跡不一致 (status=invalidated)。
EVIDENCE_INVALIDATED = "history_evidence_invalidated"
# 再評価依頼の確定した証跡不一致 (status=pending)。
EVIDENCE_REFRESH_REQUIRED = "history_evidence_refresh_required"
# 設定・一覧の読取失敗などで照合できない (status=pending)。
CHECK_UNAVAILABLE = "history_check_unavailable"

# rollback の拒否 (RollbackRefusal.reason)。
ROLLBACK_EVIDENCE_REQUIRED = "history_evidence_required"

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

REASONS = frozenset(TEXT)


def text_for(reason: object) -> str | None:
    """固定 reason の文言。知らない reason は None (呼び出し側が失敗させる)。"""
    return TEXT.get(reason) if isinstance(reason, str) else None

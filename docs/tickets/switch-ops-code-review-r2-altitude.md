---
id: switch-ops-code-review-r2-altitude
status: 設計待ち
priority: 未設定
opened: 2026-09-20
closed: null
related: [switch-ops-hardening]
backfilled: true
source_section: 未完了
---
# [switch-ops-code-review-r2-altitude]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[switch-ops-code-review-r2-altitude] 起票 2026-09-20 ([switch-ops-hardening] 2 周目 /code-review #3,4,6〜10、いずれも保守性で現挙動の欠陥ではない、`tmp/review-20260920-soh/r2/code-review.md`): lock 内再読 → 比較の手書きが 3 箇所 (`_revert_under_lock` / `_finalize_decision` / `_advance_to_decided`) — store 層の compare-and-set に寄せる余地 / `_plugin_materialize` の `except ValueError` が広い (専用例外型へ) / `still_pending` の `ApprovalOutcome` 構築が 4 箇所複製で `:1595` だけ `rolled_back_op_id` を渡さない (現状は常に None で無害) / `ApprovalOutcome.status` が生 str で outcome との整合検査が無い (`GateOutcome.verdict_kind` の Literal 流儀に) / `_approval_list` の SQL が `approvals.pending()` と重複 / payload_json の parse-with-fallback が `_approval_detail` と複製 / `_finalize_decision` のガードが同 lock 内で直前に読んだ行を再 SELECT / (2026-09-20 追記、headless `/code-review high` 試験の指摘) `_plugin_bless` と `_plugin_retire` の `UnresolvedJournalError` except ブロックが `print(...); return 1` の構造ごと重複 — `_print_journal_recovery_error(e) -> int` に一本化できる

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


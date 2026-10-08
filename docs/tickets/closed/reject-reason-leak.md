---
id: reject-reason-leak
status: 是正済
priority: 高
opened: 2026-09-12
closed: 2026-09-12
related: [profitability-floor, profitability-floor]
backfilled: true
source_section: 未完了
---
# [reject-reason-leak]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

[reject-reason-leak] **是正済 [profitability-floor] T0 (cd19457、`rejected_by_human` 固定 + 除染 7 行)** — 旧: (高、2026-09-12 実機 `tmp/approval-20260912b.md`、ユーザー承認済「すべて承認」)** 人間の reject reason が `backlog.last_result='rejected:<reason>'` → 改善履歴表 → improve プロンプトへ逐語還流 (遮断 8 ⑧ 違反、`improve_loop.py:629-633`)。是正 = `_OUTCOME_TABLE['rejected']` を固定文言 `rejected_by_human` (reason は approval_requests のみ) + pin「reason 文字列がプロンプトに現れない」。既存 5 行 (#27/#38/#39/#51/#60) の除染は `tmp/decontam-20260912.py` (ユーザー実行、classifier が委任を拒否)。**[profitability-floor] 束の T0 として実装**

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


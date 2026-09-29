---
id: tier-b-release-requires-evaluable-backtest
status: 是正済
priority: 未設定
opened: 2026-09-09
closed: 2026-09-09
related: []
backfilled: true
source_section: 未完了
---
# [tier-b-release-requires-evaluable-backtest]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[tier-b-release-requires-evaluable-backtest] 是正済み 2026-09-09** `e048c4f` → codex Important (evaluable は gate 用 trades ≥ 30、低頻度戦略が閉じ込められる) → `6f01221` 成功 = `trades > 0` (int、bool 除外) → ローカル pin 2 `b11f02f`。3507 passed。**A4 8 回目 実施 (2026-09-09、#65、`tmp/a4-run8-20260909.md`)**: **Tier F 実機初発火** — `failed reason=tool_budget_abort:max_tool_calls calls=300 refused=0` で 31 分に閉じた (timeout でない、CP1 充足)。note 型 B #41 を更新 (増殖なし、CP3 充足)。abort 経由の resume でも MCP 無効化 (CP4-d)。**ただし速い経路 (refusal streak 10) は踏めず** — モデルが `afx_analyze_corr` を 293 回連打し全て例外で失敗したため (下記 2 欠陥)。trades=0 backtest の再検証は入力が発生せず持ち越し。trade 競合ゼロ (#64 完走を待って送信)。(旧: 重要、run7 欠陥 A)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


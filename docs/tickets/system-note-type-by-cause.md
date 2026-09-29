---
id: system-note-type-by-cause
status: 是正済
priority: 未設定
opened: 2026-09-09
closed: 2026-09-09
related: []
backfilled: true
source_section: 未完了
---
# [system-note-type-by-cause]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[system-note-type-by-cause] 是正済み 2026-09-09** `e048c4f`: backtest 0 → 型 A / abort → 型 B / timeout・max_turns → 型 C (時間切れ)。(旧: 中、run7 欠陥 B) Tier D' の型 A/B 分岐が死因でなく run_backtest 件数 (n_bt) なので、予算枯渇していない timeout でも型 B (「予算が尽きたら…」) を申し送る。分岐を「reason が tool_budget_abort か」で切り、timeout は別文言 (型 C) に

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


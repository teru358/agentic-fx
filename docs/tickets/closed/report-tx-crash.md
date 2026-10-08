---
id: report-tx-crash
status: 是正済
priority: 未設定
opened: 2026-08-31
closed: 2026-08-31
related: []
backfilled: true
source_section: 是正済み
---
# [report-tx-crash]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[report-tx-crash] 是正済 `71e342d` (2026-08-31) — report_state='prepared' UPDATE の暗黙 tx を独立 tx 化 (M4 実装時に発見)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


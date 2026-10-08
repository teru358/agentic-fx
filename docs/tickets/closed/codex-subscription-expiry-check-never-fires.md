---
id: codex-subscription-expiry-check-never-fires
status: 是正済
priority: 低
opened: 2026-09-12
closed: 2026-09-12
related: []
backfilled: true
source_section: 未完了
---
# [codex-subscription-expiry-check-never-fires]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[codex-subscription-expiry-check-never-fires] **是正済 test-hygiene T4 (2026-09-12、検査撤去 + stderr fatal `usage limit`/`try again at`/`401 Unauthorized`/`Unauthorized`)** — 旧: (低、同 観測 D) `_check_codex_subscription_expiry` は実 auth.json に `chatgpt_subscription_active_until` が無く常に WARNING で空振り (裁定 R4 で fail closed にしていない)。キー名を codex 0.150.1 の実形式で再実測するか検査を撤去するか、裁定

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


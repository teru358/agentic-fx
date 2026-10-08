---
id: legacy-oc-context-limit-hardcoded
status: 是正済
priority: 未設定
opened: 2026-09-01
closed: 2026-09-01
related: []
backfilled: true
source_section: 是正済み
---
# [legacy-oc-context-limit-hardcoded]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-context-limit-hardcoded] 裁定 2026-09-01: `runner.opencode.context_limit` (初期値 0、improve.backend=opencode なら validator + factory で >0 強制)。実装 codex → 1 周目レビュー束 (80da823) に含む

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


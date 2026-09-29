---
id: legacy-oc-run-diag
status: 是正済
priority: 未設定
opened: 2026-08-31
closed: 2026-08-31
related: []
backfilled: true
source_section: 是正済み
---
# [legacy-oc-run-diag]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-run-diag]/[run_plugin_tests-EACCES] 是正済 `a59c0be` → `2a08e34` (2026-08-31) — run_plugin_tests 失敗を stderr 込みに、Landlock 下の祖先 pyproject 遮断 (`-c /dev/null`)。m24 で実機確認 (5 passed)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


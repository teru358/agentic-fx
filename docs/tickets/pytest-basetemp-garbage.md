---
id: pytest-basetemp-garbage
status: 是正済
priority: 未設定
opened: 2026-09-05
closed: 2026-09-05
related: []
backfilled: true
source_section: 未完了
---
# [pytest-basetemp-garbage]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[pytest-basetemp-garbage] **是正済 b0d27a9** — テストが残す 0500/0400 ツリーで pytest の basetemp 掃除が失敗し `/tmp/pytest-of-<user>/garbage-*` が 95 世代 × 250MB = 12GB 蓄積、/tmp クォータ枯渇で Claude Code Bash が全滅 (2026-09-05)。conftest の autouse teardown で復元

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


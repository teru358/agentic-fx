---
id: loader-none-crash
status: 是正済
priority: 未設定
opened: 2026-08-31
closed: 2026-08-31
related: []
backfilled: true
source_section: 是正済み
---
# [loader-none-crash]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[loader-none-crash] 是正済 `14ca38a`/`b513d91`/`0cd4efb` (2026-08-31) — `commit()` の `_discover_one` None を AttributeError にしない、compensate_commit_failure、gate が `loader_rejected: <理由>` で不合格。m24 で実機確認

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


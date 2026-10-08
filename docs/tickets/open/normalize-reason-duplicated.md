---
id: normalize-reason-duplicated
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [normalize-reason-duplicated]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[normalize-reason-duplicated] (低、CR5) `cli_runner._normalize_reason` が local_runner.py:39-56 の逐語複製、`_MAX_REASON_CHARS` の pin は local 側のみ。_safe_error.py に集約

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


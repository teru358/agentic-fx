---
id: latest-in-sample-metrics-requires-row-factory
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [latest-in-sample-metrics-requires-row-factory]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[latest-in-sample-metrics-requires-row-factory] (低、同 観測 E) `latest_in_sample_metrics` は `row_factory=sqlite3.Row` を暗黙に要求し、無いと黙って None

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


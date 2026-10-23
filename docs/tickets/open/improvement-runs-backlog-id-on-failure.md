---
id: improvement-runs-backlog-id-on-failure
status: 設計待ち
priority: 中
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [improvement-runs-backlog-id-on-failure]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improvement-runs-backlog-id-on-failure] (低、run7 観測 E) 失敗 mission では `improvement_runs.backlog_id` が NULL のままで選択 backlog を追えない

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-23: 優先: 中 — 2026-10-23 棚卸し: 実 DB で失敗 mission 26 件中 25 件が NULL。中へ

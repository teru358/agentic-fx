---
id: approval-no-history-passthrough
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [approval-no-history-passthrough]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[approval-no-history-passthrough] (低) `plugin/approval.py` の `_validate_kind`/`_validate_strategy` (holdout.run_in_sample の第 3 消費者) は NoHistoryError を捕捉せず素通し (旧回廊 [D-5] と同系)。gate 判定に落とすかは D-5 と束で判断

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


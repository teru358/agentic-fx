---
id: outage-observe-closed-guard-unpinned
status: 設計待ち
priority: 低
opened: 2026-09-29
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [outage-observe-closed-guard-unpinned]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[outage-observe-closed-guard-unpinned] 起票 2026-09-29 (低、テスト強度)**: `OutageStateMachine.observe` の「閉場中は観測しない」early return を落としても既存スイート (1105 本) が green (ローカル r1 トリアージで実測)。自動復帰経路がこの guard に依存するので、閉場中の tick で state/streak が変わらないことを pin する

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


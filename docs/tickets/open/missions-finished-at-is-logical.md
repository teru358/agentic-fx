---
id: missions-finished-at-is-logical
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [missions-finished-at-is-logical]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[missions-finished-at-is-logical] (低、同 観測 D) `missions.finished_at` は終端メソッドに渡す `now` で、親ゲート 3 本 (≒70 秒) の前に束縛される → 所要時間が 22% 過小。「論理終端時刻」と明記するか壁時計を別記録するか裁定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


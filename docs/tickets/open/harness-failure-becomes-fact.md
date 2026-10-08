---
id: harness-failure-becomes-fact
status: 設計待ち
priority: 中
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [harness-failure-becomes-fact]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[harness-failure-becomes-fact] (中) harness 由来の失敗 (timeout 等) をモデルが「環境制約 fact」として note 化し続ける (#24/#28/#29) → 次回以降 backtest 回避に誘導。tool error 応答に「harness 側の障害、環境制約ではない」旨を返すか、fact 化の抑止を設計

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


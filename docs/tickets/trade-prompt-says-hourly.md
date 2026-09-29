---
id: trade-prompt-says-hourly
status: 設計待ち
priority: 低
opened: 2026-09-28
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [trade-prompt-says-hourly]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[trade-prompt-says-hourly] 起票 2026-09-28 (低)**: 取引判断 prompt の冒頭「1 時間毎に呼び出され」が固定文言のまま (判断足 15m では 15 分毎)。`decision_timeframe` から「N 分毎」を生成するか文言を「判断足の確定ごとに」に

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


---
id: multi-timeframe-indicator-deps
status: 設計待ち
priority: 未設定
opened: 2026-09-15
closed: null
related: [indicator-consumption-wiring]
backfilled: true
source_section: 未完了
---
# [multi-timeframe-indicator-deps]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[multi-timeframe-indicator-deps]** (2026-09-15 起票、[indicator-consumption-wiring] §1 でスコープ外にした項目): 1 strategy が複数時間足の指標を同時に使う (`rsi_1h: {plugin: rsi, timeframe: 1h}`)。ハーネスが `load_resampled_frame` で別足 df を作り indicator に渡し、結果を strategy の足へ `asof` 整列。handshake の df が足ごとに増える IPC コストと `max_bars` の意味 (足ごとの warmup) の再定義が設計課題。注: 1 つの indicator plugin を任意の足の strategy から使うことは本束で既に可

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


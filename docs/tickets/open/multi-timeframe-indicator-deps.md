---
id: multi-timeframe-indicator-deps
title: 1 つの strategy が複数時間足の指標を同時に使えない
status: 設計待ち
priority: 低
opened: 2026-09-15
closed: null
related: [indicator-consumption-wiring]
backfilled: true
source_section: 未完了
---
# [multi-timeframe-indicator-deps]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[multi-timeframe-indicator-deps]** (2026-09-15 起票、[indicator-consumption-wiring] §1 でスコープ外にした項目): 1 strategy が複数時間足の指標を同時に使う (`rsi_1h: {plugin: rsi, timeframe: 1h}`)。ハーネスが `load_resampled_frame` で別足 df を作り indicator に渡し、結果を strategy の足へ `asof` 整列。handshake の df が足ごとに増える IPC コストと `max_bars` の意味 (足ごとの warmup) の再定義が設計課題。注: 1 つの indicator plugin を任意の足の strategy から使うことは本束で既に可

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 1 つの strategy が複数時間足の指標を同時に使えない — 仕分け (2026-10-02、現物で成立を確認): df IPC コストと max_bars の意味の再定義が設計課題。 根拠: 複数足の指標依存を扱う実装・commit が無い。

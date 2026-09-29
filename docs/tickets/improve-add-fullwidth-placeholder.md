---
id: improve-add-fullwidth-placeholder
status: 設計待ち
priority: 未設定
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [improve-add-fullwidth-placeholder]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[improve-add-fullwidth-placeholder] 起票 2026-09-27 (`/code-review high` ofc r2、低)**: `improve add` のプレースホルダ判定は ASCII `<…>`/`[…]` のみで全角 `＜案1＞`・`［TODO］` は警告されない。設計書 §3 で「広く意図する拡張は別 ticket」と明示済みの範囲外。日本語 UI では踏みやすいので拡張を検討

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


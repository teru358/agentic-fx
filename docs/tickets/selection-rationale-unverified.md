---
id: selection-rationale-unverified
title: 承認材料の selection_rationale が未検証で捏造され得る
status: 設計待ち
priority: 高
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [selection-rationale-unverified]

**状態**: 設計待ち / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

[selection-rationale-unverified] selection_rationale が未検証で payload に載る (m40 は attempts を捏造)。attempts と突き合わせで検出可

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 高、題名: 承認材料の selection_rationale が未検証で捏造され得る — 仕分け (2026-10-02、現物で成立を確認): attempts との突き合わせで検出可能、m40 で attempts を捏造した実例。 根拠: loops/improve_loop.py:1636 で output の selection_rationale を未検証のまま payload に載せる。

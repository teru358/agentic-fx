---
id: backlog-dup
title: backlog に実質同じ案が重複して溜まる
status: 設計待ち
priority: 低
opened: null
closed: null
related: [backlog-dedup-miss]
backfilled: true
source_section: 未完了
---
# [backlog-dup]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backlog-dup]/[backlog-dedup-miss] (統合) discoveries 由来の backlog #12/#13 が実質重複 (「config 許可キーは kind と params」)、#13/#16 同一事実、#15 は #11 と矛盾。idea_norm が表記揺れを吸収できていない。重複検出は未設計

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: backlog に実質同じ案が重複して溜まる — 仕分け (2026-10-02、現物で成立を確認): 重複検出は未設計、discoveries 由来の #12/#13 などが重複。 根拠: store/backlog.py:23 と improve_loop.py:1404 の idea_norm は strip().lower() のみで表記揺れを吸収しない。

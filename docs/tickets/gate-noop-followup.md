---
id: gate-noop-followup
title: fact 行が選択され得る件は kind 必須化の追随が未観測
status: 裁定待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [gate-noop-followup]

**状態**: 裁定待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[gate-noop-followup] 現行 note 化されていない fact 行が残る間は選択され得る (裁定待ち)。次回 E2E で `kind` 必須化にモデルが追随するか (schema 不遵守 → output_invalid) を観測

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 裁定待ち、優先: 低、題名: fact 行が選択され得る件は kind 必須化の追随が未観測 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): kind 必須化は入ったが、モデルの追随と既存 fact 行の扱いは未確認。 根拠: src/agentic_fx/loops/summary.py:116 で kind が必須化済み。ただし『note 化されていない fact 行が残る間』の挙動と次回 E2E でのモデル追随は未観測。E2E 観測で決まる。

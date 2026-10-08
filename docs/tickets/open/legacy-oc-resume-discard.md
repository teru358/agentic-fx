---
id: legacy-oc-resume-discard
title: resume 時の report 降格経路が未発火で未検証
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-resume-discard]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-resume-discard 残] M4 の report 降格経路は未発火 (m28 はモデルが自発的に observation を出したため) — 別途 report 型で確認要

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: resume 時の report 降格経路が未発火で未検証 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 観測性は入ったが降格経路は E2E 未確認。 根拠: resume 追撃の主因は 5dd7fd0 等で是正済み。残りの report 降格経路 (M4) は未発火で、report 型の mission を走らせて確認する必要がある。

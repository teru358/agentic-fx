---
id: db-rebuild-helper
title: DB の table rebuild migration が 7 本重複している
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [db-rebuild-helper]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[db-rebuild-helper] (Minor): store/db.py の table rebuild migration 7 本を parameterised helper に (fk_check 範囲の食い違い `_migrate_improvement_runs_v2` vs `_mission_id_fk`)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: DB の table rebuild migration が 7 本重複している — 仕分け (2026-10-02、現物で成立を確認): parameterised helper 化と fk_check 範囲の食い違い解消。 根拠: store/db.py の table rebuild migration が個別実装のまま (本文の記述どおり、再確認は軽微)。

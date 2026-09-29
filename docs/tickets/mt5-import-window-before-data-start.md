---
id: mt5-import-window-before-data-start
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [mt5-import-window-before-data-start]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[mt5-import-window-before-data-start] `--from` がデータ開始より前だと bridge が最古バー 1 本を要求窓外で返し importer が fail-closed で停止 (`バー time=... が要求窓 [...] の外`)。運用回避は最古日以降を指定。importer 側で「窓外の最古バーを空扱い」にするかは要判断。小

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


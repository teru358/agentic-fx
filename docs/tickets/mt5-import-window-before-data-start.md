---
id: mt5-import-window-before-data-start
title: --from がデータ開始より前だと importer が停止する
status: 裁定待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [mt5-import-window-before-data-start]

**状態**: 裁定待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[mt5-import-window-before-data-start] `--from` がデータ開始より前だと bridge が最古バー 1 本を要求窓外で返し importer が fail-closed で停止 (`バー time=... が要求窓 [...] の外`)。運用回避は最古日以降を指定。importer 側で「窓外の最古バーを空扱い」にするかは要判断。小

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 裁定待ち、優先: 低、題名: --from がデータ開始より前だと importer が停止する — 仕分け (2026-10-02、現物で成立を確認): 窓外の最古バーを空扱いにするかの判断が要る。運用回避は最古日以降を指定。 根拠: mt5_import.py:152-157 が窓外のバーで ValueError を出す fail-closed のまま。

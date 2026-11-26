---
id: history-sync-next-advises-import-when-rerun-suffices
title: history sync の next 文が、再実行で通る場合にも import を案内する
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-26
closed: null
related: [live-confirmed-bars-into-history]
---
# [history-sync-next-advises-import-when-rerun-suffices] history sync の next 文が、再実行で通る場合にも import を案内する

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

2026-11-26 の手動補充で、1 回目の `afx history sync --rescan-holdout --until-done` が参照銘柄の recent を inspection_insufficient で deferred にし「比較できる既存の足が足りません。主 pair と参照を同じ開始日から afx history import で導入し…」と案内した。実際は同じ実行の gap queue で日ごとの足が埋まり、再実行 2 回で recent も通った。比較用履歴が本当に無い場合と、同じ実行の gap 修復で比較対象ができて再実行で通る場合を判別して案内を分ける。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-26: 起票。

---
id: web-layout-tabs
title: 同梱 web の各画面をタブに分け、plugin と improve の表を並べ替え可能にする
status: 実装中
priority: 中
kind: 機能
opened: 2026-11-15
closed: null
related: [web-plugin-improve-hub, web-ui-charts-equity-indicators]
---
# [web-layout-tabs] 同梱 web の各画面をタブに分け、plugin と improve の表を並べ替え可能にする

**状態**: 実装中 / **優先**: 中 / **種別**: 機能

## 現象

画面は節を縦に積んだ仮置きで、1 ページ 64 KiB を全節で分け合い、並びはアルファベット順。承認一覧は nav に無く、plugin・improve の表は並べ替えられない。書込みの form がある画面は自動再読込が外れる

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-15: 起票。
- 2026-11-15: 状態: 設計待ち — 設計 C0: docs/superpowers/specs/2026-11-15-web-layout-tabs-design.md
- 2026-11-15: 状態: 実装中 — 設計 C2.1 を利用者が承認 (2026-11-15)。task: T1 タブの土台と Status/Activity / T2 Plugins と plugin 詳細と一覧の並べ替え / T3 Improve / T4 承認詳細 (仮想ページ・決定のブロック)。worktree tmp/wt/weblayout

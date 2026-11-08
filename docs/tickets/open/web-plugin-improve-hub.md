---
id: web-plugin-improve-hub
title: 同梱 web に plugin と改善の画面群を作る (一覧・依存・承認・改善の実行)
status: 実装中
priority: 高
kind: 機能
opened: 2026-11-08
closed: null
related: [improve-run-summary-for-humans, web-ui-charts-equity-indicators, web-https-for-writes, improve-runner-choice]
---
# [web-plugin-improve-hub] 同梱 web に plugin と改善の画面群を作る (一覧・依存・承認・改善の実行)

**状態**: 実装中 / **優先**: 高 / **種別**: 機能

## 現象

web の承認一覧は pending を並べるだけで、plugin の配備状態・strategy と indicator の依存・改善の様子が見えない。承認と改善の実行は Discord と CLI でしかできない (2026-11-08 利用者要件)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-08: 起票。
- 2026-11-08: 関連: [improve-run-summary-for-humans, web-ui-charts-equity-indicators, web-https-for-writes, improve-runner-choice] — 段 A (読む) を main に merge (8e122365): plugin の read model (純関数、CLI afx plugin list と共有)、依存の列挙、有用性 (提出件数・配備成功件数)、GET /v1/plugins・/v1/plugins/{name}、web の /plugins・/plugins/{name} とナビ Plugins。レビュー codex 1 周目・/code-review high・閉鎖確認 2 回。残り: /improve の画面 (要約の束の後)、段 B1 (loopback の書込み、実装前レビュー済み、設計 C2.3)

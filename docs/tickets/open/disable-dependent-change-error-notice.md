---
id: disable-dependent-change-error-notice
title: disable 後に依存 strategy が解決不能になる旨の表示が無い
status: 設計待ち
priority: 低
opened: 2026-10-18
closed: null
related: [deployed-plugin-has-no-disable-or-rollback]
---
# [disable-dependent-change-error-notice] disable 後に依存 strategy が解決不能になる旨の表示が無い

**状態**: 設計待ち / **優先**: 低

## 現象

spec v1.0 の D11 にあった「dependent が新たに解決不能なら次回起動で not_found になる旨を ERROR 表示」は未実装で、v1.1 で spec から外して後続にした。unlink 自体は依存に影響されず done になる (安全側)。表示だけの診断面。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-18: 起票。

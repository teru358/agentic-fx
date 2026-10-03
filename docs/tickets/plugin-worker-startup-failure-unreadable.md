---
id: plugin-worker-startup-failure-unreadable
title: plugin worker の起動失敗の理由が読めない
status: 設計待ち
priority: 中
opened: 2026-10-03
closed: null
related: []
---
# [plugin-worker-startup-failure-unreadable] plugin worker の起動失敗の理由が読めない

**状態**: 設計待ち / **優先**: 中

## 現象

worker が plugin import 前に落ちると traceback が残らず、Landlock の適用失敗も plugin_error (候補の責任) に分類される。

## 原因

## 処置案・裁定

起動失敗を sandbox_unavailable など候補の責任でない分類に分け、理由を技術ログに残す。plugin worker Landlock の束で扱う。

## 修正内容

## 経緯

- 2026-10-03: 起票。

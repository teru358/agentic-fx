---
id: plugin-lock-wait-notice-on-wait-argument
title: plugin lock の待機通知を on_wait 引数に置き換える
status: 設計待ち
priority: 低
opened: 2026-10-18
closed: null
related: [deployed-plugin-has-no-disable-or-rollback]
kind: 保守
---
# [plugin-lock-wait-notice-on-wait-argument] plugin lock の待機通知を on_wait 引数に置き換える

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

deploy CLI の「他の操作の完了を待っています」通知は switch.set_plugin_lock_wait_notice の process-global callback (context manager で前値復元) で実装した。CLI と daemon は別 process なので実害は無いが、同 process の並行呼び出しでは無関係な lock 取得でも通知が出る。lock API (_plugin_locks / acquire_op_locks) を触る機会に on_wait 引数へ置き換える。spec v1.1 §14 の後続。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-18: 起票。
- 2026-10-25: 種別: 保守 — 種別を付与

---
id: manual-improve-runs-synchronously-on-caller-thread
title: 手動 improve が呼び出し元のスレッドで mission 全体を同期実行する
status: 設計待ち
priority: 中
opened: 2026-10-03
closed: null
related: []
---
# [manual-improve-runs-synchronously-on-caller-thread] 手動 improve が呼び出し元のスレッドで mission 全体を同期実行する

**状態**: 設計待ち / **優先**: 中

## 現象

ImproveSupervisor.submit_manual (core/improve_supervisor.py:117-143) は mission 全体を呼び出し元のスレッドで走らせる。対話シェルの improve はその間固まり、停止時の join の対象にも入らない。操作 API では job 化が必須。

## 原因

## 処置案・裁定

専用スレッドで走らせ、同時 1 本の旗と停止時 join に入れる。シェルの improve は「起動しました (job …)」を返す形に (操作 API の T3)。

## 修正内容

## 経緯

- 2026-10-03: 起票。

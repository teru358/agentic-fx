---
id: scheduler-tick-watchdog-health-check-test-fails
title: scheduler tick が watchdog の健全性チェックを呼ぶ test が main で落ちる
status: 設計待ち
priority: 中
opened: 2026-10-24
closed: null
related: []
---
# [scheduler-tick-watchdog-health-check-test-fails] scheduler tick が watchdog の健全性チェックを呼ぶ test が main で落ちる

**状態**: 設計待ち / **優先**: 中

## 現象

tests/test_service_app.py::test_scheduler_tick_actually_invokes_the_watchdog_health_check が main 5bedd01 (2026-10-24) で単独実行でも失敗する (calls が空、約 18 秒待ち)。設計書 §6 の相互監視 (scheduler → watchdog) が片方向になっているか、test の tick 待ちが実時計に依存して環境で落ちるかの切り分けが必要。10/22 の判定では既存失敗 2 本是正後に緑だったので、94e8341 (停止の join) 以降の退行の可能性。web-lan T1 / improve-fb T1 の判定とは無関係

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-24: 起票。

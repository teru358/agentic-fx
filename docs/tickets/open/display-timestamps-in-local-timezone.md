---
id: display-timestamps-in-local-timezone
title: 人が読む面の時刻を現地 tz で表示し、bridge.log も UTC に揃える
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-11-02
closed: null
related: []
---
# [display-timestamps-in-local-timezone] 人が読む面の時刻を現地 tz で表示し、bridge.log も UTC に揃える

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

activity.log / agentic.log は UTC、mt5_bridge/logs/bridge.log は JST、afx ctl log / web / Discord は UTC 表示で、人が読むときに分かりづらい (2026-11-02 ユーザー指摘)。処置: 保存・ファイルは UTC のまま offset 明示、bridge.log も UTC に。表示面 (afx ctl log / activity / status、web、Discord) は設定 display.timezone (既定はシステム tz) で換算し tz 名を付ける

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-02: 起票。

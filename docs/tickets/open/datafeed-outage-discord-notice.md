---
id: datafeed-outage-discord-notice
title: MT5 不通 (degraded) と復帰待ちを Discord に通知する
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-31
closed: null
related: []
---
# [datafeed-outage-discord-notice] MT5 不通 (degraded) と復帰待ちを Discord に通知する

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

OANDA の TOTP 必須化 (2026-10-31) で MT5 ターミナルの再ログインは人手でしか復旧しない。datafeed の停止判定 (data_outage_degraded の confirmed、datafeed_recovered_awaiting_resume、data_outage_unprocessed_bars) は activity にしか書かれず Discord には届かないため、取引停止が続いても人が気づけない。通知は固定語で数値なし、episode あたり 1 回 (confirmed 時) + 復帰待ち 1 回。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。

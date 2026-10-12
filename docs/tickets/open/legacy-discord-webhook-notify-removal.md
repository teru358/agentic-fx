---
id: legacy-discord-webhook-notify-removal
title: 旧 discord.enabled + .env webhook の簡易通知を廃止する
status: 設計待ち
priority: 低
opened: 2026-10-12
closed: null
related: [first-run-setup]
---
# [legacy-discord-webhook-notify-removal] 旧 discord.enabled + .env webhook の簡易通知を廃止する

**状態**: 設計待ち / **優先**: 低

## 現象

service には旧 discord.enabled + DISCORD_WEBHOOK_URL の簡易通知が配線されたまま。ops の notify (generic HTTPS webhook、T8) と二系統で、同じ webhook へ二重投稿になり得る。ウィザードは ops notify だけを案内し旧経路は質問しない (first-run spec v1.0 段 8)。廃止時は settings の discord セクションと service の配線を外し、example と docs を更新。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-12: 起票。

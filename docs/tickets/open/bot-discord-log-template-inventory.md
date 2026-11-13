---
id: bot-discord-log-template-inventory
title: discord.py 内部の log を安全な固定 code に写す保守契約を作る
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-14
closed: null
related: [bot-gateway-disconnect-is-silent]
---
# [bot-discord-log-template-inventory] discord.py 内部の log を安全な固定 code に写す保守契約を作る

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

pinned の discord.py 2.7.1 の logger name と record.msg の template を棚卸しし、必要な診断だけを固定 code に写す allowlist を作る。args、整形済みの message、body、URL、exc_info、traceback、heartbeat blocked の動的な suffix は出さず、反復は上限つきの counter に畳む。依存を更新するたびに inventory と秘密の canary を回すことを保守条件にする。bot の自己監視では discord logger を閉じたまま gateway の状態機械の固定 code で足りるので、この棚卸しを前提にしない。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。

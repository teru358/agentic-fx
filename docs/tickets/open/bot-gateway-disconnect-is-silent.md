---
id: bot-gateway-disconnect-is-silent
title: bot が Discord から切断されたまま黙って生き続ける
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-11-13
closed: null
related: [auto-killswitch-latch-emits-no-event]
---
# [bot-gateway-disconnect-is-silent] bot が Discord から切断されたまま黙って生き続ける

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

afx-ops@bot の process は active のまま Discord gateway への接続が無く、bot が offline になっていた (2026-11-13 に発見、再起動で復旧)。discord.py の再接続 (connect(reconnect=True)) に任せており、discord の logger は NullHandler のため切断・再接続失敗が journal に何も残らない。process は dumpable でなく外から stack も fd も見えないので、原因の時刻も特定できなかった。直し方の案: discord の logger の WARNING 以上を journal へ出す。gateway が一定時間 (例: 10 分) READY/RESUMED でなければ BotFailure で落とし systemd に再起動させる。afx status / web に bot の gateway 状態を出す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。

---
id: legacy-e2e-ttl
title: llama-swap の TTL 120 秒で mission 中にモデルが unload される
status: 裁定待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-ttl]

**状態**: 裁定待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-ttl] llama-swap TTL=120s と improve mission の相性: アイドル 2 分でモデル unload、30B 再ロード 1〜2 分がターン毎に挟まり得る。TTL 延長 (llama-swap 側設定、ユーザー裁定) or mission 中の keepalive を検討

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 裁定待ち、優先: 低、題名: llama-swap の TTL 120 秒で mission 中にモデルが unload される — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): TTL 延長か keepalive かをユーザーが決める。 根拠: llama-swap 側の TTL 設定 (リポジトリ外) の確認とユーザー裁定が要る。keepalive 実装は src に見当たらない。

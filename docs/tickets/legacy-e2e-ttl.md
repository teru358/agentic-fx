---
id: legacy-e2e-ttl
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-ttl]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-ttl] llama-swap TTL=120s と improve mission の相性: アイドル 2 分でモデル unload、30B 再ロード 1〜2 分がターン毎に挟まり得る。TTL 延長 (llama-swap 側設定、ユーザー裁定) or mission 中の keepalive を検討

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


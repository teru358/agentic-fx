---
id: rpc-call-helper
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [rpc-call-helper]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[rpc-call-helper] (Minor): mission_worker の `_RagRpcProxy._call` / `_make_rpc_client.call` の framing 2 重実装を 1 helper に (dead `seq` 行削除)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


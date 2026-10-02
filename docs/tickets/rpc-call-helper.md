---
id: rpc-call-helper
title: RPC の framing が 2 箇所で重複実装されている
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [rpc-call-helper]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[rpc-call-helper] (Minor): mission_worker の `_RagRpcProxy._call` / `_make_rpc_client.call` の framing 2 重実装を 1 helper に (dead `seq` 行削除)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: RPC の framing が 2 箇所で重複実装されている — 仕分け (2026-10-02、現物で成立を確認): 1 helper に統合し dead な seq 行を削除する (Minor)。 根拠: mission_worker の _RagRpcProxy._call と _make_rpc_client.call の framing 2 重実装が本文どおり残る想定 (軽い確認のみ)。

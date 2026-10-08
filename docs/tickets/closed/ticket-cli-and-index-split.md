---
id: ticket-cli-and-index-split
title: チケット操作を plugin tickets に移し、完了分を CLOSED.md へ分離
status: 是正済
priority: 中
opened: 2026-09-30
closed: 2026-09-30
related: []
---
# [ticket-cli-and-index-split] チケット操作を plugin tickets に移し、完了分を CLOSED.md へ分離

**状態**: 是正済 / **優先**: 中

## 現象

起票・状態変更が手編集で、状態名や修正内容の記入漏れを防ぐ仕組みが無い。INDEX.md に完了分が残り続け、読むたびに肥大化する (42 KB 中 10 KB が是正済)。

## 原因

## 処置案・裁定

## 修正内容

- 2026-09-30: 操作は Claude Code plugin tickets (github.com/teru358/claude-tickets 0.1.0) の ticket.py に移した (new / set / close / list / index、状態・slug・修正内容必須・秘密文字列拒否を強制)。道具は本リポジトリに置かない。INDEX.md は未完了のみ、完了分は CLOSED.md に月別。scripts/tickets_index.py は削除

## 経緯

- 2026-09-30: 起票。
- 2026-09-30: 是正内容 — 操作は Claude Code plugin tickets (github.com/teru358/claude-tickets 0.1.0) の ticket.py に移した (new / set / close / list / index、状態・slug・修正内容必須・秘密文字列拒否を強制)。道具は本リポジトリに置かない。INDEX.md は未完了のみ、完了分は CLOSED.md に月別。scripts/tickets_index.py は削除

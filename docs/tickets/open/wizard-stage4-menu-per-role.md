---
id: wizard-stage4-menu-per-role
title: 段 4 の LLM 構成メニューを役割ごとの 2 問に揃える
status: 設計待ち
priority: 低
opened: 2026-10-14
closed: null
related: [first-run-setup]
---
# [wizard-stage4-menu-per-role] 段 4 の LLM 構成メニューを役割ごとの 2 問に揃える

**状態**: 設計待ち / **優先**: 低

## 現象

段 4 の 4 択 (1 取引 Claude / 2 取引 Claude + 改善 Codex / 3 別ホスト llama-swap の URL / 4 後で) は「取引と改善の束」「URL 入力」「後回し」の軸が混ざり読みにくい。改善 backend は独立に選べる実装になったので、メニューを「取引判断の backend (claude / local=URL 入力 / 後で)」「改善の backend (claude / codex / opencode / local / 後で)」の 2 問に揃える。取引は local|claude のまま (opencode は改善用ハーネスで取引には足さない)。llama-swap 不応答時に local を既定にしない規則は維持。2026-10-14 stream-svr 受入 1 回目のユーザー指摘

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。

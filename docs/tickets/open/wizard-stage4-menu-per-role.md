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

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

段 4 の 4 択 (1 取引 Claude / 2 取引 Claude + 改善 Codex / 3 別ホスト llama-swap の URL / 4 後で) は「取引と改善の束」「URL 入力」「後回し」の軸が混ざり読みにくい。改善 backend は独立に選べる実装になったので、メニューを「取引判断の backend (claude / local=URL 入力 / 後で)」「改善の backend (claude / codex / opencode / local / 後で)」の 2 問に揃える。取引は local|claude のまま (opencode は改善用ハーネスで取引には足さない)。llama-swap 不応答時に local を既定にしない規則は維持。2026-10-14 stream-svr 受入 1 回目のユーザー指摘

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。
- 2026-10-14: 優先: 中 — 受入 1 回目の実物の流れで追加: (1) 取引側の消費確認の文が 2 行重複 (「試験できます。枠を少し使います」の直後に「試験します。枠を少し使います。送りますか」)、(2) llama-swap が応答しないのに改善側の選択肢に「1. local」が出る (応答しないときは出さないか「応答なし」を添える)、(3) 「ローカル LLM では opencode が本線で、local は最低線です」は内部用語で利用者向けでない、(4) 「実往復を確定の後に試験しますか」は意味が取りにくく、取引側の消費確認の直後に 2 度目の消費確認が来る (「設定を書き終えた後に 1 回だけ試験する」と言い直し、消費確認は 1 か所に)、(5) モデル表の説明語 (fable「別系統」) の見直し。opencode 1 本化 (trade-backend-opencode-single-local-path) と合わせて段 4 を再設計する
- 2026-10-29: 優先: 低 — 2026-10-29 棚卸し: 道筋の最後 (ウィザードの見栄え)。本文は中

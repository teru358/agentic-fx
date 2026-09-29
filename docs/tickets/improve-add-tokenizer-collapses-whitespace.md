---
id: improve-add-tokenizer-collapses-whitespace
status: 設計待ち
priority: 低
opened: 2026-09-21
closed: null
related: [ops-first-contact-fixes, ops-ui]
backfilled: true
source_section: 完了ログ
---
# [improve-add-tokenizer-collapses-whitespace]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improve-add-tokenizer-collapses-whitespace] (低、2026-09-21 設計レビュー r2): シェルの tokenizer (`line.strip().split()` → `" ".join`) が全角空白・連続空白を ASCII 空白 1 個に畳んでから backlog に保存する。[ops-first-contact-fixes] では範囲外 (表示は「登録された課題文」を見せる、と定義)。課題文を 1 行まるごと受ける入力経路は [ops-ui] で。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


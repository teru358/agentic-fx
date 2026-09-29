---
id: legacy-e2e-model
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-model]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-model] `runner.<lane>.model` が backend 非分離 — backend 切替のたびにモデル名の手編集が要る (chatgpt=codex 系 / claude=claude 系 / local=llama-swap 系)。backend 毎の model 設定 or chatgpt では `-m` 省略 (既定 gpt-5.6-sol) を設計判断へ。§0.2 既定見直し提案と合流

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


---
id: legacy-e2e-model
title: backend を切り替えるたびにモデル名の手編集が要る
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-model]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-model] `runner.<lane>.model` が backend 非分離 — backend 切替のたびにモデル名の手編集が要る (chatgpt=codex 系 / claude=claude 系 / local=llama-swap 系)。backend 毎の model 設定 or chatgpt では `-m` 省略 (既定 gpt-5.6-sol) を設計判断へ。§0.2 既定見直し提案と合流

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: backend を切り替えるたびにモデル名の手編集が要る — 仕分け (2026-10-02、現物で成立を確認): backend 毎の model 設定か chatgpt で -m 省略かを設計判断する。 根拠: settings.yaml.example:26-27 は lane ごとに model を 1 つ持つだけで、backend ごとの model 切替は無い。

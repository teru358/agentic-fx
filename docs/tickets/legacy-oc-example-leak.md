---
id: legacy-oc-example-leak
title: プロンプトの実例 idea がそのまま backlog に実在化する
status: 是正済
priority: 低
opened: null
closed: 2026-10-03
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-example-leak]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-example-leak] プロンプト実例の idea「RSI の期間を 14 から 21 に…」が discoveries へ逐語コピーされ backlog #3 として実在化 (m13) — 実例の内容汚染。実例を明示プレースホルダ化する是正要

## 修正内容

- 2026-10-03: improve_mission.md の最終出力実例を <…> プレースホルダに置き換え、逐語コピー禁止の 1 行を追加

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: プロンプトの実例 idea がそのまま backlog に実在化する — 仕分け (2026-10-02、現物で成立を確認): 実例を明示プレースホルダにする是正が未実施。 根拠: loops/prompts/improve_mission.md:113 の実例 idea に実在しそうな文言 (RSI の期間を 14 から 21) が残る。
- 2026-10-03: 是正内容 — improve_mission.md の最終出力実例を <…> プレースホルダに置き換え、逐語コピー禁止の 1 行を追加

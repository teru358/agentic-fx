---
id: scheduler-bars-per-tick-memo
title: 複数ペア化すると tick ごとの価格取得が注文数に比例して増える
status: 実装待ち
priority: 中
opened: 2026-09-06
closed: null
related: [pair-rules-vs-data-availability]
backfilled: true
source_section: 完了ログ
---
# [scheduler-bars-per-tick-memo]

**状態**: 実装待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[scheduler-bars-per-tick-memo]** (2026-09-06 /code-review): live tick が open 行・pending 行ごとに `bars_fn(pair)` (ネットワーク fetch + upsert commit) を 2N+M+1 回呼ぶ (`core/scheduler.py:416/938/989/1023`)。tick 先頭で pair ごと 1 回 fetch して dict で配る設計。live 性能、Important。 **裁定 2026-09-06: 現状 (pairs=[USDJPY]) はそのまま。ただし複数ペア取引の前提条件 — pairs を 2 つ以上にする前に必ず実装する (fetch 回数が pair × 注文数で線形増、外向き予算)。[pair-rules-vs-data-availability] / EURUSD 有効化と束で。**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 中、題名: 複数ペア化すると tick ごとの価格取得が注文数に比例して増える — 仕分け (2026-10-02、現物で成立を確認): pairs を 2 つ以上にする前に必ず実装する前提条件 (2026-09-06 裁定)。現状 1 ペアでは実害なし。 根拠: scheduler.py:621,635,711,1318 が行ごとに bars_fn を呼ぶ。tick 先頭で pair 単位に配る仕組みは無い。

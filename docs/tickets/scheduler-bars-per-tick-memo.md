---
id: scheduler-bars-per-tick-memo
status: 設計待ち
priority: 未設定
opened: 2026-09-06
closed: null
related: [pair-rules-vs-data-availability]
backfilled: true
source_section: 完了ログ
---
# [scheduler-bars-per-tick-memo]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[scheduler-bars-per-tick-memo]** (2026-09-06 /code-review): live tick が open 行・pending 行ごとに `bars_fn(pair)` (ネットワーク fetch + upsert commit) を 2N+M+1 回呼ぶ (`core/scheduler.py:416/938/989/1023`)。tick 先頭で pair ごと 1 回 fetch して dict で配る設計。live 性能、Important。 **裁定 2026-09-06: 現状 (pairs=[USDJPY]) はそのまま。ただし複数ペア取引の前提条件 — pairs を 2 つ以上にする前に必ず実装する (fetch 回数が pair × 注文数で線形増、外向き予算)。[pair-rules-vs-data-availability] / EURUSD 有効化と束で。**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


---
id: signal-producer-catchup-requires-latest-tail
status: 設計待ち
priority: 中
opened: 2026-09-28
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [signal-producer-catchup-requires-latest-tail]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[signal-producer-catchup-requires-latest-tail] 起票 2026-09-28 (中、要再現、astra 指摘)**: producer は過去 bucket を順に評価 (`signal_producer.py:195`) しながら、毎回 now 基準の最新 df 末尾との一致を要求 (`:242`) するため、複数 bucket の catch-up が途中で止まり得る。再現テストから

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


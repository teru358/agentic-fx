---
id: fake-run-context-consolidation
title: テストのフェイク RunContext が散在し追随漏れで壊れやすい
status: 設計待ち
priority: 低
opened: 2026-09-17
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [fake-run-context-consolidation]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[fake-run-context-consolidation] 起票 2026-09-17 (段 0 束 3 提案 5): `tests/runners/test_worker_runner.py` のフェイク Mission / `_FakeRunContext` が 6 箇所に散在し、handshake キーを足すたびに `{}` で追随するだけになる (T5a `c26f92b` の tests/runners 破損の再発形)。1 箇所へ集約 + handshake キー集合を 1 本で逐語 pin。1 周目是正では見送り (リファクタ)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: テストのフェイク RunContext が散在し追随漏れで壊れやすい — 仕分け (2026-10-02、現物で成立を確認): 1 箇所へ集約し handshake キー集合を 1 本で pin する整理が未実施。 根拠: tests/runners/test_worker_runner.py:3367,3443 ほかに _FakeRunContext が散在し、tests/loops にも複数ある。

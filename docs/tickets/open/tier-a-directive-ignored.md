---
id: tier-a-directive-ignored
title: 自己テストの連続失敗の警告が無視され続け、止める仕組みが無い
status: 設計待ち
priority: 中
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [tier-a-directive-ignored]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[tier-a-directive-ignored] (重要、run9 観測 A) Tier A の repeated_failure directive は 9 回連続で無視された。Tier A には abort trigger が無い。同一署名の連続失敗を recoverable streak に載せるか、Tier A を「誘導」から外して Tier C の予算に任せるかの設計判断

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 中、題名: 自己テストの連続失敗の警告が無視され続け、止める仕組みが無い — 仕分け (2026-10-02、現物で成立を確認): 同一署名の連続失敗を abort に載せるか、Tier A を誘導から外すかの設計判断。 根拠: improve_staging_tools.py:389-405 は directive を返すだけで、mission_counters.py:138 の連続失敗カウントは abort に繋がらない。

---
id: market-tool-budget-persist-via-alert-state
title: 市場ツール予算超過の警告が再起動で重複する
status: 設計待ち
priority: 低
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [market-tool-budget-persist-via-alert-state]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[market-tool-budget-persist-via-alert-state] 起票 2026-09-27 (B-2 段 d、見送り、低)**: `context_daily_call_budget` 超過の WARNING は warned latch がプロセス内メモリのみで、再起動で同日に再警告する。alert_state での永続化は集計層が conn_core を触れないため見送り。併記: `_market_tool_daily` は無ロック (supervisor 直列のため現状安全、並列化したら要ロック)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 市場ツール予算超過の警告が再起動で重複する — 仕分け (2026-10-02、現物で成立を確認): 永続化は集計層が conn_core を触れず見送り、並列化時は要ロック。 根拠: loops/trade_loop.py:95,706 の _market_tool_daily はプロセス内メモリのみで再起動で再警告。

---
id: cancel-stall-in-cancelling-state
title: 取消の途中で DB 遷移に失敗した注文が CANCELLING に残り予約を占有する
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-16
closed: null
related: [killswitch-latch-pending-limit-fills]
---
# [cancel-stall-in-cancelling-state] 取消の途中で DB 遷移に失敗した注文が CANCELLING に残り予約を占有する

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

Executor.cancel_order は broker 取消が ok の後に CANCELLED へ遷移するが、その DB 遷移が例外になると行は CANCELLING のまま残る。CANCELLING は exposure (予約リスク・件数) に算入されるが、unknown 解決の走査対象ではないため再走査されず、予約枠を恒久に占有する。取消理由 (market_close / reservation / rate_unavailable / account_unknown / equity_nonpositive / llm_cancel / kill_switch) に共通。broker が rejected を返した PROTECTION_PENDING も、それを進める保護走査が無い (現行 PaperBroker.cancel は常に ok なので未到達)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-16: 起票。

---
id: reference-literal-symbol-allow-trading-pairs
title: 戦略の参照 symbol に取引 pair も許す (pairs + watch_symbols)
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-31
closed: null
related: [reference-symbols-as-indicator-inputs]
---
# [reference-literal-symbol-allow-trading-pairs] 戦略の参照 symbol に取引 pair も許す (pairs + watch_symbols)

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

resolver は IndicatorRef の literal symbol を watch_symbols に限る (symbol_not_watched)。取引 pair は watch に入れられない (overlap) ので、pairs=[USDJPY, EURUSD] のとき USDJPY の戦略から EURUSD を参照できない。spec は settings membership としか言わず、pairs の key は参照されても hard のまま、と取引 pair の参照を想定している。許可集合を pairs + watch_symbols (read_symbol_param と同じ) に広げる。2026-10-31 ユーザー裁定。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。

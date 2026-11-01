---
id: improve-analyze-corr-schema-and-symbol-decl-undocumented
title: analyze_corr の形と symbol: 宣言が agent に伝わらない
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-11-01
closed: null
related: []
---
# [improve-analyze-corr-schema-and-symbol-decl-undocumented] analyze_corr の形と symbol: 宣言が agent に伝わらない

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

2026-10-31 02:21 以降 36 mission で同じ行き止まり。analyze_corr の tool schema は request: object と説明「親が相関分析を回す。」だけで、親の検証 (kind ∈ corr_matrix / rolling_corr_summary / lead_lag、キー集合の完全一致、timeframe 15m/1h/4h/1d、window 20/60/120、a/b は reference_symbols) が agent に見えない。prompt に analyze_corr の使い方も symbol: 宣言の形 (indicators の各項目に symbol / timeframe、生系列は plugin: ohlcv) も無く、配備済み strategy に使用例も無い。agent は symbols/lookback/lag 形式を推測して invalid_request、観測「symbol 宣言は見つからず」を繰り返す。処置: tool parameters を kind ごとの明示 schema に、prompt 7 項に symbol: 宣言と analyze_corr の例を足す (数値・期間は出さない)、schema と _REQUEST_SCHEMA の同期 pin

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-01: 起票。

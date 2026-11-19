---
id: backtest-avg-r-currency-mismatch
title: backtest の avg_r が口座通貨の損益を quote 通貨のリスク額で割る
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-19
closed: null
related: [web-ui-charts-equity-indicators]
---
# [backtest-avg-r-currency-mismatch] backtest の avg_r が口座通貨の損益を quote 通貨のリスク額で割る

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

`S/backtest/metrics.py:94-96` の R は `pnl / (|約定 − SL| × 数量 × 契約単位)`。分子の pnl は口座通貨 (`S/core/paper_broker.py:17-32` と backtest の rate_fn で換算)、分母は quote 通貨のまま。quote 通貨が口座通貨と違う pair (例: 口座 JPY で EURUSD) では換算率の倍だけ R がずれ、床の require_positive_avg_r・承認の数字・baseline_monitor に効く。今は pairs が USDJPY だけ (quote = 口座通貨) なので表に出ていない。非 JPY quote の pair を有効にする前に、分母を同じ時点の換算率で口座通貨にするか、換算できない注文を R 不明にする。2026-11-19 チャートの段の設計レビューで発見

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-19: 起票。

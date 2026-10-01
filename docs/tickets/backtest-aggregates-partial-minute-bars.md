---
id: backtest-aggregates-partial-minute-bars
title: backtest と resample が欠けた 1m 足をある分だけ集約し、部分的な足を作る
status: 設計待ち
priority: 低
opened: 2026-10-01
closed: null
related: []
---
# [backtest-aggregates-partial-minute-bars] backtest と resample が欠けた 1m 足をある分だけ集約し、部分的な足を作る

**状態**: 設計待ち / **優先**: 低

## 現象

broker は tick が無かった分の 1m 足を作らない (USDJPY で 60 日に 96 分、他銘柄は時間帯を問わず発生)。backtest と一部の resample は欠けた 1m を「ある分だけ」集約するため、上位足の OHLC と指標が部分的な足から計算される。live の判断足は MT5 から別取得なので影響しない。

## 原因

集約時に構成する 1m 足の本数・完全性を見ていない (設計レビューでの指摘、現物の再確認は着手時に行う)。

## 処置案・裁定

欠落率か部分足フラグを持たせるか、判断用途では完全性を要求するかを決める。無 tick の分は価格が動いていないので実害は小さい見込みだが、未検証。

## 修正内容

## 経緯

- 2026-10-01: 起票。

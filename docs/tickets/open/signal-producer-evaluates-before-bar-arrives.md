---
id: signal-producer-evaluates-before-bar-arrives
title: signal_producer が 1h 足の確定前に評価して毎時 WARNING を出す
status: 設計待ち
priority: 中
opened: 2026-10-10
closed: null
related: [multi-timeframe-indicator-deps, signal-producer-hourly-warning-before-confirmation]
---
# [signal-producer-evaluates-before-bar-arrives] signal_producer が 1h 足の確定前に評価して毎時 WARNING を出す

**状態**: 設計待ち / **優先**: 中

## 現象

plugin sma_cross_10_30 (USDJPY, 1h) の評価が毎時 00 分すぎ (取引 mission の起動時) に走り、対象 bucket (直前の 1h 足) がまだ mt5-live に無いため「evaluation failed at bucket … (target bucket not yet present in mt5-live data)」の WARNING を出す。2026-09-20 12:06 UTC (yfinance 時代) 以降 4,383 回、現在も毎時 1 回。実害は判断足 15m の mission で 1h 戦略のシグナルが 1 時間遅れる可能性と、ログの汚れ。原因候補: 評価の起点が mission 開始時刻で、1h 足の ingest 完了 (確定足の取込) を待っていない。処置案: 評価を「対象 bucket が揃ったとき」に遅延・再試行する (次の 15 分 mission で再評価されるなら WARNING を INFO に落とす)、または mission 起動前に 1h 足の ingest を先に完了させる。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-10: 起票。
- 2026-10-23: 関連: [multi-timeframe-indicator-deps, signal-producer-hourly-warning-before-confirmation] — 重複チケット signal-producer-hourly-warning-before-confirmation を本件に統合 (2026-10-23 棚卸し)

---
id: reference-series-additional-sources
title: 参照系列の取得先を MT5 以外に広げる (金利など)
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-10-28
closed: null
related: [reference-symbols-as-indicator-inputs]
---
# [reference-series-additional-sources] 参照系列の取得先を MT5 以外に広げる (金利など)

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

参照系列の本束 (設計 2026-10-25-reference-series-design.md) は MT5 ブローカーが配信する symbol に限る (2026-10-28 ユーザー方針)。本機のブローカーは 67 銘柄で、指数 CFD (JP225 US500 US100 US30 US2000 GER30 UK100 EU50 HK50 CHINA50 など) と商品 (XAUUSD XAGUSD USOIL UKOIL NATGAS COPPER など) はあるが、債券・金利と USD index は無い。候補: (1) 金利は FRED の日次 (例 DGS10、無料、API key 要) を「公表済みの前日値だけ使う」規則で、日中の動きは見えない。(2) yfinance (^TNX など) は非公式で、週明けの取得穴の前例があり非推奨。(3) DXY は MT5 の 6 構成通貨ペア (EURUSD USDJPY GBPUSD USDCAD USDSEK USDCHF、すべて本機ブローカーにある) から ICE の算出式で合成でき、取得先を増やさずに作れる (MT5 内の合成系列として別に扱ってよい)。論点: 取得先ごとの時計と公表時刻の整合 (通貨ペアと比べるときのタイムラグの扱い)、外向きリクエストの予算、key の保管、backtest と live の同じ規則、失敗時の扱い。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-28: 起票。

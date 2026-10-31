---
id: multi-input-indicator-relations
title: 多入力 indicator: 2 銘柄の生系列から関係 (相関・スプレッド・β) を指標として計算する
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-31
closed: null
related: [reference-symbols-as-indicator-inputs, reference-literal-symbol-allow-trading-pairs]
---
# [multi-input-indicator-relations] 多入力 indicator: 2 銘柄の生系列から関係 (相関・スプレッド・β) を指標として計算する

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

現状の indicator は 1 系列の frame しか受けず、2 銘柄の関係を見る戦略は strategy 側で自前計算するしかない。指標同士 (EMA と EMA) の関係では各指標の窓・平滑化の差が混ざり何の関係か言えないので、生の 2 系列を入力に取る 1 つの indicator として計算するのが直接的 (2026-10-31 ユーザー方針)。設計方針: (1) indicator plugin は対象銘柄を固定しない (現状どおり。多入力でも plugin 側は名前付き入力 a, b… を宣言するだけ)、(2) 銘柄・timeframe・max_age の束ね方は使う側の strategy 宣言で決める (例 inputs: {a: {symbol: self}, b: {symbol: EURUSD}})、(3) 整列・鮮度・K_ref・backtest preflight は参照 frame の仕組みを流用、(4) family key / payload / 監査表示に入力の束ね方を含める、(5) 改善ループの prompt と例に多入力 indicator の書き方を載せる。状態: 設計待ち (C0 は baseline T4 と ref-literal の閉鎖後)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。
- 2026-10-31: 状態: 設計待ち — 2026-10-31 ユーザー裁定で起票。C0 は現在の 2 束の後

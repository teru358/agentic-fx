---
id: improve-duplicate-self-computed-strategies
title: 自前計算の戦略 (sma_cross 型) は重複判定を構造変更で避けられる
status: 設計待ち
priority: 低
opened: 2026-10-22
closed: null
related: [candidates-converge-to-example-sma, strategy-attribution-and-shadow-evaluation]
kind: 不具合
---
# [improve-duplicate-self-computed-strategies] 自前計算の戦略 (sma_cross 型) は重複判定を構造変更で避けられる

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

spec 2026-10-19-improve-loop-quality-design §11 の既知の未解決穴。indicator を使わず自前で計算する戦略は仮説族 (indicator 構成 + 足 + pair) が空になり、ロジック等価 (AST 構造 hash) もループの書き換えなどの構造変更で避けられる。candidates-converge-to-example-sma の収束形 (example sma_cross の写し) はこの型。候補の是正案: backtest の entry bar 集合の Jaccard による発火時刻の一致判定 (影の台帳と同じ材料)、または自前計算の戦略への構造的な近さの判定。着手は strategy-attribution-and-shadow-evaluation の後

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-22: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与

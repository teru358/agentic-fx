---
id: web-decision-close-vs-hold-r-compare
title: 判断による決済と SL/TP まで持った場合の R の比較を見せる
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-11-19
closed: null
related: [web-ui-charts-equity-indicators]
---
# [web-decision-close-vs-hold-r-compare] 判断による決済と SL/TP まで持った場合の R の比較を見せる

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

判断 (LLM) で決済した取引について、そのまま SL/TP まで持った場合の R を OHLCV の再生で求めて比べたい (2026-11-17 の手作業の分析では判断決済が約 2.9R 良かった)。約定の規則の再利用、同じ足で SL と TP の両方に届いたときの扱い、価格の欠け、決済後どこまで追うか、を別に設計する。対象が少ないうちは結論を出しにくい。web-ui-charts-equity-indicators の設計 §6 の問い 1 で範囲外とした (2026-11-19)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-19: 起票。

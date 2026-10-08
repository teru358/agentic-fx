---
id: risk-gate-rr-uses-configured-spread
title: risk gate の RR 計算が設定値の spread を使い、実 spread と乖離する
status: 設計待ち
priority: 低
opened: 2026-10-01
closed: null
related: [plugin-sees-live-spread]
---
# [risk-gate-rr-uses-configured-spread] risk gate の RR 計算が設定値の spread を使い、実 spread と乖離する

**状態**: 設計待ち / **優先**: 低

## 現象

risk gate は RR (リスクリワード) の計算に設定上の想定 spread を使う。実 spread が想定より大きく広がっている間 (ロールオーバー前後など) は、計算上の RR が実態より良く出る。

## 原因

実 spread がアプリ内に保持されておらず、設定値しか参照できない (設計レビューでの指摘、現物の再確認は着手時に行う)。

## 処置案・裁定

plugin-sees-live-spread で実 spread を保持した後に、RR 計算へ実測値を使うか・設定値との大きいほうを使うかを決める。本体の計算の正確さの問題で、戦略の判断とは別。

## 修正内容

## 経緯

- 2026-10-01: 起票。

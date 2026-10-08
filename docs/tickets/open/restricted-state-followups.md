---
id: restricted-state-followups
title: restricted 状態の残り: 欠落の記録、入力契約のテスト、結合検証
status: 設計待ち
priority: 低
opened: 2026-10-02
closed: null
related: [outage-stalled-on-broker-daily-rollover-gap]
---
# [restricted-state-followups] restricted 状態の残り: 欠落の記録、入力契約のテスト、結合検証

**状態**: 設計待ち / **優先**: 低

## 現象

spec の task 2・3 が未実装。後から分かる 1m 足の穴を品質の記録として残す仕組み、signal 生成が上位足だけを読むことと読み取りツールが穴のある 1m を加工せず返すことの契約テスト、週末・signal の戻し・障害注入を通した結合検証。

## 原因

## 処置案・裁定

spec の「task 分割」節の T2・T3。状態の遷移と発注の停止は変えない。

## 修正内容

## 経緯

- 2026-10-02: 起票。

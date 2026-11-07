---
id: reevaluation-approval-detail-display-polish
title: 再評価の承認詳細の数値が丸められず、旧形式の注記と分類「不明」が出る
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-07
closed: null
related: [redo-deployed-strategy-evaluation-after-time-repair]
---
# [reevaluation-approval-detail-display-polish] 再評価の承認詳細の数値が丸められず、旧形式の注記と分類「不明」が出る

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

2026-11-07 の本番の再評価 (#89〜#94) の承認詳細で次を観測。(1) pf などが pf=1.0165433487012487 のように丸められない。(2) 再評価の依頼にも「旧形式の承認依頼です.親の事実表は保存されていません」の注記が出る。(3) 承認後の #89 だけ分類が「不明」(他は同一版)。(4) 評価中に maintain_reservations failed: ValueError: no completed 5m bar for rate conversion at 2026-06-02T21:25:00Z の警告 1 行 (評価は完走)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-07: 起票。

---
id: backtest-history-snapshot-reproducibility
title: 履歴が変わった後も同じ入力で backtest を再現できるようにする
status: 設計待ち
priority: 高
kind: 機能
opened: 2026-11-14
closed: null
related: [improve-eval-window-reference-availability, live-confirmed-bars-into-history]
---
# [backtest-history-snapshot-reproducibility] 履歴が変わった後も同じ入力で backtest を再現できるようにする

**状態**: 設計待ち / **優先**: 高 / **種別**: 機能

## 現象

評価区間の設計は run の端点と許可参照集合を固定し、1 回の gate 内の候補・guard・基準線を同じ read snapshot で読むが、run 全体の複数 backtest・別 gate・全件再評価の間や、後日の再実行の入力集合は固定しない。通常 import、同期による古い gap の修復、time repair、人間の置換で行や値が変わり、端点と件数の記録だけでは結果を再現できない。段 3 の定期同期で入力差が日常的になる。不変コピー、履歴 watermark / ingest_seq、版指定の as-of などを比べ、run 全体の入力識別・保持・再実行・保存費用・秘密の遮断・epoch による失効の責務を設計する。同数の値変更も扱い、同じ入力指定なら変更の前後で同じ測定になることを受入条件にする。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。

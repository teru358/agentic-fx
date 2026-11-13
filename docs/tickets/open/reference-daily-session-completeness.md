---
id: reference-daily-session-completeness
title: 非24時間銘柄の1d参照を取引セッションに沿って確定する
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-14
closed: null
related: [improve-eval-window-reference-availability]
---
# [reference-daily-session-completeness] 非24時間銘柄の1d参照を取引セッションに沿って確定する

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

参照日足は UTC の 24 個の 1h 時刻がすべて揃うことを要求しており、非24時間銘柄の US500・JP225・USOIL では全期間、XAUUSD では調査時点の in-sample に実効 1d が無い。XAUUSD の全期間 32 日は 2026-09-21 以後に偏り、原因は未確認。商品・broker の取引セッション・休止時間・DST と確定時刻を調べ、live producer と backtest が同じ日足完全性・終端を使う契約を設計する。未来参照や休日の補間は入れず、非24時間・短縮取引・DST・セッションまたぎの fixture で検証する。評価区間の設計は使えない座標を外すだけでこの欠陥を直さない。日足規則を変えたら評価区間の規則版と U/S を測り直す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。

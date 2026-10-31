---
id: indicator-symbol-agnostic-and-duplicate-gate
title: indicator の銘柄非依存と重複を決定論 gate で検査する
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-31
closed: null
related: [multi-input-indicator-relations, reference-symbols-as-indicator-inputs]
---
# [indicator-symbol-agnostic-and-duplicate-gate] indicator の銘柄非依存と重複を決定論 gate で検査する

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

改善ループが作る indicator は特定銘柄に向く傾向がある (名前に銘柄、pip_size や価格差の閾値を param に持つ、JPY スケールの既定値)。契約上 indicator は銘柄を持たないが、中身がスケール依存だと別銘柄に束ねたとき黙って壊れる。また既存 indicator と同等のものが繰り返し作られる。指示 (directives) では抑えきれないので決定論の gate にする (2026-10-31 ユーザー方針)。設計方針: (1) 名前・param 名・docstring に pairs + watch_symbols の銘柄名が入れば reject (固定語)、(2) スケールの違う 2 銘柄分の canonical fixture で実行し両方で有限・非定数の出力が出ること、価格を k 倍した系列で出力が不変 or k 倍のどちらにも収まらないものは scale_dependent warning (承認画面に表示、reject はしない)、(3) param に価格単位 (pips / pip_size / price_*) を要求しない、(4) 重複: 既存 indicator と canonical fixture 上で出力が一致または相関 0.99 超なら indicator_duplicate で提出不可、inventory に「既存 X と同等」を残す (strategy の族に相当する仕組みを indicator に足す)、(5) 改善 prompt に規則を 1 文。多入力 indicator より先に入れる。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。
- 2026-10-31: 状態: 設計待ち — 2026-10-31 ユーザー裁定で起票。C0 は baseline T4 / ref-literal の閉鎖後、multi-input より先

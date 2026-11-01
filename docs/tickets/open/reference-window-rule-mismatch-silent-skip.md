---
id: reference-window-rule-mismatch-silent-skip
title: 参照窓の規則が設定時と実行時で違い strategy が毎バー黙って skip
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-11-01
closed: null
related: []
---
# [reference-window-rule-mismatch-silent-skip] 参照窓の規則が設定時と実行時で違い strategy が毎バー黙って skip

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

参照系列 (symbol: 宣言) を使う strategy の backtest が閾値を緩めても取引 0 件 (2026-11-01 mission 2700 / 2710 / 2711 / 2712)。設定時 resolve.nominal_reference_bars は取引バー数で数え (400 ≤ 400 で通る)、実行時 reference.runtime_reference_window_reason は主 df の壁時計の経過を参照足で数える (400 本の 1h は週末を含み約 544 時間 > 400) ため、df が満長になると strategy_adapter が毎バー None を返して skip し、理由は backtest 結果に出ない。処置: 実行時も設定時と同じ数え方 (整列した参照バーの実数) にする、skip 理由 reference_window_exceeded を backtest 結果と activity に出す、週末跨ぎの 400 本窓を pin する test。CPU 予算 (sandbox_session_cpu_sec、参照 frame を毎バー送る重さ、mission 2697 / 2713 / 2715 / 2717 の worker_cpu_limit) は是正後の実測で別チケット

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-01: 起票。

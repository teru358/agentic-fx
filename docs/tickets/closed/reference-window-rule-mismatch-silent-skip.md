---
id: reference-window-rule-mismatch-silent-skip
title: 参照窓の規則が設定時と実行時で違い strategy が毎バー黙って skip
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-01
closed: 2026-11-01
related: []
---
# [reference-window-rule-mismatch-silent-skip] 参照窓の規則が設定時と実行時で違い strategy が毎バー黙って skip

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

参照系列 (symbol: 宣言) を使う strategy の backtest が閾値を緩めても取引 0 件 (2026-11-01 mission 2700 / 2710 / 2711 / 2712)。設定時 resolve.nominal_reference_bars は取引バー数で数え (400 ≤ 400 で通る)、実行時 reference.runtime_reference_window_reason は主 df の壁時計の経過を参照足で数える (400 本の 1h は週末を含み約 544 時間 > 400) ため、df が満長になると strategy_adapter が毎バー None を返して skip し、理由は backtest 結果に出ない。処置: 実行時も設定時と同じ数え方 (整列した参照バーの実数) にする、skip 理由 reference_window_exceeded を backtest 結果と activity に出す、週末跨ぎの 400 本窓を pin する test。CPU 予算 (sandbox_session_cpu_sec、参照 frame を毎バー送る重さ、mission 2697 / 2713 / 2715 / 2717 の worker_cpu_limit) は是正後の実測で別チケット

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-01: 実行時も主 df の窓に入る参照バーの実数 (取引バー) で数え、判定は reference_window_fits の 1 関数を設定時と共有。backtest の agent 応答に skipped_bars_by_reason (件数のみ)、live の reference_unavailable 通知は (strategy, hash, reason) × UTC 日で 1 回。週末跨ぎ 400 本 pin、399 拒否、skip 件数、missing、日次 dedupe の test。spec reference-series v0.6.8。plugin 2693 / core 2299 / backtest+loops 2534 緑。CPU 予算は別チケット

## 経緯

- 2026-11-01: 起票。
- 2026-11-01: 是正内容 — 実行時も主 df の窓に入る参照バーの実数 (取引バー) で数え、判定は reference_window_fits の 1 関数を設定時と共有。backtest の agent 応答に skipped_bars_by_reason (件数のみ)、live の reference_unavailable 通知は (strategy, hash, reason) × UTC 日で 1 回。週末跨ぎ 400 本 pin、399 拒否、skip 件数、missing、日次 dedupe の test。spec reference-series v0.6.8。plugin 2693 / core 2299 / backtest+loops 2534 緑。CPU 予算は別チケット

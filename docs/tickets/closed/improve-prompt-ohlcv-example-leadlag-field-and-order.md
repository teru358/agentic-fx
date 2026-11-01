---
id: improve-prompt-ohlcv-example-leadlag-field-and-order
title: improve prompt の ohlcv 例・lead_lag の trial_count・lock 後の順序
status: 是正済
priority: 中
kind: 不具合
opened: 2026-11-01
closed: 2026-11-01
related: []
---
# [improve-prompt-ohlcv-example-leadlag-field-and-order] improve prompt の ohlcv 例・lead_lag の trial_count・lock 後の順序

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象

2026-11-01 の改善 24 mission で 3 種の行き止まり。(1) prompt 7 項の例 plugin: ohlcv は配備済みに無く indicator_unresolved (6 mission)。設計は人間が docs/examples/plugins/ohlcv を配備する前提だったが未配備で、今は agent 製 reference_ohlcv が配備済み (ohlcv を bless すると exact 重複)。(2) lead_lag の応答 trial_count=25 (LAGS -12..12 の本数) を agent が標本数と読み「標本 25」で先行関係を棄却 (12 回)。(3) lock_staging_deps 後の self-test は成功 backtest (取引 ≥ 1) が無いと run_backtest_required_first で拒否されるが prompt 7 項は self-test → backtest の順。処置: 例を「配備済みの通過 indicator (outputs が open/high/low/close/volume) を宣言」に、agent 面の trial_count を lags_tested に改名し整列観測数 n_obs を返す、7 項の順序を backtest → self-test に、2 銘柄同時入力の indicator は未対応 (multi-input-indicator-relations) と明記

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-01: prompt 7 項の例から plugin: ohlcv を外し「配備済みの通過 indicator (outputs が OHLCV) を symbol: 付きで宣言」に、lock 後の順序を run_backtest → run_plugin_tests に、2 系列同時入力の indicator は未対応と明記。lead_lag の agent 応答は trial_count を外し lags_tested (25) と n_obs (ピーク lag の整列観測数) を返す。loops / tools / backtest 2977 緑

## 経緯

- 2026-11-01: 起票。
- 2026-11-01: 是正内容 — prompt 7 項の例から plugin: ohlcv を外し「配備済みの通過 indicator (outputs が OHLCV) を symbol: 付きで宣言」に、lock 後の順序を run_backtest → run_plugin_tests に、2 系列同時入力の indicator は未対応と明記。lead_lag の agent 応答は trial_count を外し lags_tested (25) と n_obs (ピーク lag の整列観測数) を返す。loops / tools / backtest 2977 緑

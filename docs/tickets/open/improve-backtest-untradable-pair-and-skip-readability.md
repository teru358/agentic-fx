---
id: improve-backtest-untradable-pair-and-skip-readability
title: 改善の backtest が取引できない pair を黙って 0 取引にし、参照の除外の読み方も伝わらない
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-11-08
closed: null
related: [backtest-worker-cpu-budget-shrinks-with-timeframe, improve-run-summary-for-humans]
---
# [improve-backtest-untradable-pair-and-skip-readability] 改善の backtest が取引できない pair を黙って 0 取引にし、参照の除外の読み方も伝わらない

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

2026-11-08 の手動の改善 #3377・#3382 (EURUSD 主軸 + US500 参照) が 0 取引・評価不能になり、agent は参照系列の欠損が原因と申告した。実際は EURUSD が settings.pairs に無く、open の意図がすべて not_tradable_symbol で却下されていた (参照で除外は 1.2%)。run_backtest の応答に却下理由も評価した足数も出ないため、agent が誤って帰属する。参照の除外自体は正しい挙動 (参照市場の休場・warmup) だが、USDJPY 主軸では参照の履歴が無い先頭 952 足 (12.2%) が評価区間に入っている。調査: tmp/inv-refgap/report.md

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-08: 起票。

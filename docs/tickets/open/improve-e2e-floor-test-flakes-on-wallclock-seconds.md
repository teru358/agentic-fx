---
id: improve-e2e-floor-test-flakes-on-wallclock-seconds
title: improve e2e の床 test が activity 行の時刻の秒に数字が当たると落ちる
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-07
closed: null
related: []
---
# [improve-e2e-floor-test-flakes-on-wallclock-seconds] improve e2e の床 test が activity 行の時刻の秒に数字が当たると落ちる

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

tests/loops/test_improve_e2e.py::test_strategy_profitability_floor_reroutes_to_gate_failed_unprofitable は activity の行全体に '40' が含まれないことを見ている。行頭の時刻の秒が 40 のとき (例 02:47:40) に落ちる (2026-11-07 の判定で観測、単独再実行は緑)。本文だけを見るように直す

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-07: 起票。

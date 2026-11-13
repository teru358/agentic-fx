---
id: killswitch-latched-activity-repeats
title: 同一 kill switch 世代で繰り返される latch activity を一件にする
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-14
closed: null
related: [auto-killswitch-latch-emits-no-event]
---
# [killswitch-latched-activity-repeats] 同一 kill switch 世代で繰り返される latch activity を一件にする

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

open intent を同じ latched 世代で評価するたびに kill_switch_latched の activity が記録される。StateStore に原子的な latch API と changed の結果を設け、executor は changed のときだけ現行の文面で記録し、intent ごとの gate_rejected は残す。replay の RecordingStateStore の override と二重記録の防止を同時に設計し、基準 revision で「ラッチ → 拒否の反復 → 自動解除 → 再ラッチ」の fixture を保存して、BacktestResult の kill_switch_events・kill_switch_latches、orders・fills、equity・account、PF・drawdown・件数・改善 gate、解除時刻・replay_ks_rebase を canonical な byte で比べて互換を確かめる。通知の sweep とは独立に完了を判定する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。

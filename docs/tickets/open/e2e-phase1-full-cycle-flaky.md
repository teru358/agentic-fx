---
id: e2e-phase1-full-cycle-flaky
title: e2e phase1 full_cycle が sleep 0.5 の競合で時々落ちる
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-10-30
closed: null
related: []
---
# [e2e-phase1-full-cycle-flaky] e2e phase1 full_cycle が sleep 0.5 の競合で時々落ちる

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

2026-10-30 21:3x、main (3fdcc88e 以降) と baseline-gate worktree の両方で、単独実行でも通ったり落ちたりする (assert len(rows) == 1、rows が空)。test は supervisor スレッドが job を実行するのを time.sleep(0.5) で待っており、負荷が高いと間に合わない。判定束で偽の赤を出す。待ちを条件待ち (job 完了の event / polling with deadline) に替える

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-30: 起票。

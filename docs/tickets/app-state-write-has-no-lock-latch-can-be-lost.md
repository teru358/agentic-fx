---
id: app-state-write-has-no-lock-latch-can-be-lost
title: kill switch の状態ファイルの書き込みに lock が無い
status: 実装中
priority: 高
opened: 2026-10-03
closed: null
related: []
---
# [app-state-write-has-no-lock-latch-can-be-lost] kill switch の状態ファイルの書き込みに lock が無い

**状態**: 実装中 / **優先**: 高

## 現象

StateStore.update は lock なしの read-modify-write で、一時ファイル名も固定 (app_state.tmp、store/state.py:75-83)。対話シェルの killswitch reset と executor のラッチ (trade_loop.py:346 の core_lock 区間) が重なると、ラッチが消えるか、壊れた JSON で次の起動が止まり得る。daemon に操作 API を載せると daemon でも同じ窓が開く。

## 原因

## 処置案・裁定

StateStore にプロセス内 lock と一意の一時名を足す。解除は「ラッチ中のときだけ」の比較更新にする。操作 API の T0 として実装 (tmp/design-ops-api/C0.md §6)。

## 修正内容

## 経緯

- 2026-10-03: 起票。
- 2026-10-03: 状態: 実装中 — 2026-10-03 着手 (並行実装、Landlock 設計の収束待ちの間)。

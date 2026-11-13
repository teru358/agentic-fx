---
id: killswitch-reset-hwm-rebase
title: kill switch の手動解除で HWM を付け直す明示操作が無い
status: 設計待ち
priority: 高
kind: 機能
opened: 2026-11-13
closed: null
related: [auto-killswitch-latch-emits-no-event]
---
# [killswitch-reset-hwm-rebase] kill switch の手動解除で HWM を付け直す明示操作が無い

**状態**: 設計待ち / **優先**: 高 / **種別**: 機能

## 現象

live の kill switch reset は latch を解くだけで HWM を付け直さない。HWM は含み益込みなので、実現損の後に建玉が無いと DD が閾値以上のまま残り、解除しても次の open intent の gate 評価で必ず再ラッチする (2026-11-13 の解除後がこの状態)。利用者裁定 (2026-11-13 R4): 通知の設計とは分け、損失予算を人が切り下げる明示操作 (例: 通常の reset と別の rebase 操作、確認文・監査つき) として本チケットで設計する。replay の自動解除の rebase との意味の違いも整理する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。

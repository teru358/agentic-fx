---
id: indicator-update-dependent-relock-flow
title: 指標更新時に依存する戦略の再ロックを手順化する
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-27
closed: null
related: [approval-base-identity-cas, plugin-naming-lifecycle-policy]
---
# [indicator-update-dependent-relock-flow] 指標更新時に依存する戦略の再ロックを手順化する

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

指標が新版で承認されると、その指標を pin した戦略は pin 不一致になり、新規 signal 保存と新規エントリーが拒否される (plugin/source_state.py、signal_producer.py、trade_loop.py の signal_source_inactive)。再起動後は第 2 相で除外。取引全体は止まらないが、その戦略は再ロック (複製 → pin だけ更新 → 再提出、plugin/resolve.py の is_relock_transition) まで止まる。依存列挙は plugin/deploy_deps.py にある。指標の承認時に影響する戦略を示す、再ロック候補を作る、段階展開する、などの手順と利用者への見せ方を決める。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
- 2026-10-29: 優先: 中、関連: [approval-base-identity-cas, plugin-naming-lifecycle-policy] — 2026-10-29 棚卸し: 指標更新で依存戦略の pin が不一致になり新規エントリー拒否。同名更新を前提にする plugin-naming 方針と同時に決める

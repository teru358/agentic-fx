---
id: cli-launch-malformed-frames-order-dependent
title: cli_launch の malformed frame 試験が実行順に依存
status: 設計待ち
priority: 低
opened: 2026-10-10
closed: null
related: []
kind: 不具合
---
# [cli-launch-malformed-frames-order-dependent] cli_launch の malformed frame 試験が実行順に依存

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

tests/core/test_cli_launch.py::test_malformed_frames_are_rejected[go_before_prepared-attestation_order] が tests/core 全体の実行順でのみ落ちる。単独・部分集合では緑。T3b 由来、base adc26eb で再現 (2026-10-10 観測)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-10: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与

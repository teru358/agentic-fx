---
id: improve-repeat-shutdown-test-flaky
title: improve_repeat の shutdown test が束でだけ落ちる (flaky)
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-10-31
closed: null
related: []
---
# [improve-repeat-shutdown-test-flaky] improve_repeat の shutdown test が束でだけ落ちる (flaky)

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

tests/ops/test_improve_repeat_integration.py::test_shutdown_before_the_first_mission_ends_the_controller_with_shutdown が tests/ops 束の中で failed != done で落ち、単独では 22 passed (2026-10-31 に 2 回、別 pytest が並走中)。時間依存の疑い。原因未特定。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。

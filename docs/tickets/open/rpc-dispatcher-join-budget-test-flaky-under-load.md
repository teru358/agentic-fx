---
id: rpc-dispatcher-join-budget-test-flaky-under-load
title: rpc dispatcher の join budget 試験が高負荷時に揺れる
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: [canary-reject-flaky-port-reuse-forward]
---
# [rpc-dispatcher-join-budget-test-flaky-under-load] rpc dispatcher の join budget 試験が高負荷時に揺れる

**状態**: 設計待ち / **優先**: 低

## 現象

tests/runners/test_worker_runner.py::test_rpc_dispatcher_join_budget_dominates_real_elapsed_when_rpc_handler_hangs が full suite (並列 subagent + /tmp 逼迫中) で 1 回失敗: 2 件目の submit が dispatcher 終了前に accepted=True で受理された (期待は reason=running の拒否)。単独 3 回は pass。handler の hang を timing (rpc_timeout_sec=0.1) で作っており負荷で順序が崩れる。原因は 2026-09-27 b0de51f の試験設計、本体の退行ではない。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。

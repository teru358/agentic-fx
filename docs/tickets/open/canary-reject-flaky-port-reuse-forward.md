---
id: canary-reject-flaky-port-reuse-forward
title: canary の reject 試験が稀に port_reuse_forward も失敗項目に含めて落ちる
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: [real-cli-selftest-opencode-does-not-stop]
---
# [canary-reject-flaky-port-reuse-forward] canary の reject 試験が稀に port_reuse_forward も失敗項目に含めて落ちる

**状態**: 設計待ち / **優先**: 低

## 現象

tests/core/test_improve_canary.py::test_canary_failure_blocks_exec_under_real_isolation が稀に detail=listen_ipv4_eperm,port_reuse_forward となり listen_ipv4_eperm 単独と一致しない (2026-10-11、backend 3 本化で canary 実行回数が増え露出頻度が上がった。同 test 3 回連続は 9/9 通過)。原因は src/agentic_fx/core/improve_canary.py の port_reuse_forward 項目 (375-396 付近) のタイミング依存の疑い、未調査。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。

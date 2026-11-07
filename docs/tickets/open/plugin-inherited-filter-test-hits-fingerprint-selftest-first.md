---
id: plugin-inherited-filter-test-hits-fingerprint-selftest-first
title: 継承 seccomp filter の拒否 test が fingerprint 自己試験の失敗を先に返す
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-07
closed: null
related: []
---
# [plugin-inherited-filter-test-hits-fingerprint-selftest-first] 継承 seccomp filter の拒否 test が fingerprint 自己試験の失敗を先に返す

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

tests/plugin/test_plugin_worker_protocol.py::test_parent_with_a_real_inherited_filter_never_calls_popen が main で落ちる。期待は code=sandbox_unavailable / reason=inherited_seccomp_filter / popen_calls=0 だが、実際は reason=runtime_fingerprint_selftest_failed / popen_calls=1。PluginSession の入口で runtime fingerprint の自己試験が継承 filter の検査より先に子を起動している疑い。検査順の退行か test の前提の古さかは未確定 (2026-11-07 の全範囲判定で発見)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-07: 起票。

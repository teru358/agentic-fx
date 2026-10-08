---
id: supervisor-cleanup-test-leaks-36-fds
title: worker supervisor の cleanup テストが 1 回で fd を 36 本増やす
status: 設計待ち
priority: 低
opened: 2026-10-03
closed: null
related: [full-suite-fd-over-1024-breaks-shell-tests]
---
# [supervisor-cleanup-test-leaks-36-fds] worker supervisor の cleanup テストが 1 回で fd を 36 本増やす

**状態**: 設計待ち / **優先**: 低

## 現象

全スイートの fd 増加を追うと tests/runners/test_worker_runner.py::test_supervisor_reports_running_through_worker_cleanup_then_accepts の [local] と [claude] で各 +36 本の跳ねがある (2026-10-03、flake 束の計測)。他は緩やかに増える。fd が 1024 を超えると shell の select() が落ちる (full-suite-fd-over-1024-breaks-shell-tests)。

## 原因

未特定。テストか本体の supervisor cleanup 経路で pipe/fd を閉じていない可能性。

## 処置案・裁定

単独実行で /proc/self/fd を前後比較し漏れ元を特定、本体側なら是正、テスト側なら fixture で閉じる。

## 修正内容

## 経緯

- 2026-10-03: 起票。

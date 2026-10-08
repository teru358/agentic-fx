---
id: full-suite-fd-over-1024-breaks-shell-tests
title: 全体テストで fd が 1024 を超え shell 系 10 件と init 1 件が落ちる
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: [supervisor-cleanup-test-leaks-36-fds]
---
# [full-suite-fd-over-1024-breaks-shell-tests] 全体テストで fd が 1024 を超え shell 系 10 件と init 1 件が落ちる

**状態**: 設計待ち / **優先**: 中

## 現象

tests --ignore=tests/loops を 1 本で回すと 11 failed (test_shell_interrupt 9、test_service_app の stop_event 1、test_init_and_guard の unreachable_bridge 1)。main (333d4d2) でも同じ 11 件で再現 (2026-10-02)。3 ファイル単独なら 208 passed。shell 系の生ログは ValueError: filedescriptor out of range in select()。init 系は期待 mt5: ConnectError に対し実際は mt5: JSONDecodeError。

## 原因

shell 系: 長い実行でテストが fd を閉じずに溜め、番号が 1024 を超えて select() が使えなくなる。漏らしているテストは未特定。本体の対話シェルも select() を使うなら、fd の多い実プロセスで同じ失敗が起き得る (未確認)。init 系: テストが実際の localhost:8812 に当たり、稼働中の bridge の応答に依存している。

## 処置案・裁定

(1) fd を漏らしているテスト群を特定して閉じる (2) 本体の読み取りが select() なら poll() に替える (worker 側は既に poll) (3) init のテストは到達不能な宛先を固定で使い、実 bridge に当たらないようにする。

## 修正内容

## 経緯

- 2026-10-02: 起票。
- 2026-10-03: 関連: [supervisor-cleanup-test-leaks-36-fds] — 2026-10-03: shell 系は shell.py の select.poll 化で解消 (fd 1100 超での pin テスト追加)。残は init 系 (実 bridge :8812 依存、test-init-offline-unreachable-bridge-flake) と fd 増加の元 (supervisor-cleanup-test-leaks-36-fds)。
- 2026-10-03: 関連: [supervisor-cleanup-test-leaks-36-fds] — 2026-10-03: shell 系は shell.py の select.poll 化で解消 (fd 1100 超での pin テスト追加)。残は init 系 (実 bridge :8812 依存、test-init-offline-unreachable-bridge-flake) と fd 増加の元 (supervisor-cleanup-test-leaks-36-fds)。

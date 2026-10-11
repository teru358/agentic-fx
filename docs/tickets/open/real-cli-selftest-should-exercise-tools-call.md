---
id: real-cli-selftest-should-exercise-tools-call
title: 実 CLI 自己試験に副作用のない tools/call を 1 回必須にする
status: 設計待ち
priority: 高
opened: 2026-10-11
closed: null
related: [real-cli-selftest-opencode-does-not-stop]
---
# [real-cli-selftest-should-exercise-tools-call] 実 CLI 自己試験に副作用のない tools/call を 1 回必須にする

**状態**: 設計待ち / **優先**: 高

## 現象

test_real_cli_selftest / verify_backend は prompt の往復・MCP initialize・非空 tools/list を成功条件にし、prompt で tool 呼出しを禁じている。tools/call の要求→spool→dispatcher→応答→CLI 受領の経路は自己試験では未観測で、tools/list が正常でも tools/call の応答が欠落する故障を通し得る。閉鎖案: 副作用のない list_staging を 1 回必須に、応答 publish 後の tools/call 成功を worker event で観測し成功条件へ加える。改善 CLI 隔離の有効化後、R11 の selftest 定義 (spec v1.8) の改訂として扱う。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。
- 2026-10-11: 優先: 高 — 2026-10-11 実機: 隔離下の実 improve mission 1065 (codex) で code-mode host の起動失敗により tools/call が全滅したが、自己試験 (initialize + tools/list) は合格して admitted=yes だった。自己試験が tools/call を 1 回含まないと、この種の故障は有効化後も admission を通る。

---
id: flake-rpc-timeout-by-kind
status: 実装待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [flake-rpc-timeout-by-kind]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[flake-rpc-timeout-by-kind] `tests/runners/test_worker_runner.py:1129` 種別 timeout テストが sleep 0.5 vs 0.2/2.0 秒の実時間依存 (codex 1 周目 M2、10 倍マージンあり、生存変異なし)。負荷下で落ちたら他 flake と束で処理

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


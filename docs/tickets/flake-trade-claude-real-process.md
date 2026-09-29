---
id: flake-trade-claude-real-process
status: 実装待ち
priority: 未設定
opened: 2026-09-20
closed: null
related: [switch-ops-hardening]
backfilled: true
source_section: 未完了
---
# [flake-trade-claude-real-process]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[flake-trade-claude-real-process] 起票 2026-09-20 ([switch-ops-hardening] 1 周目是正のフルスイート): `tests/runners/test_worker_runner.py::test_trade_claude_real_process_completes_via_factory_build_runner` (fake claude CLI の実プロセス起動) が全スイート負荷下で 1 回 fail、単独 0.78s で pass、同一コマンド再実行で 4304 passed。束の変更とは無関係 (触っていないファイル)。再発頻度を見る

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


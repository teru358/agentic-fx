---
id: legacy-e2e-codex-sigtrap
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-codex-sigtrap]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-codex-sigtrap] codex+chatgpt の improve worker 環境下で codex の tool 実行基盤 (exec_command) が SIGTRAP 死し MCP tool も呼べない (mission #7 実測、モデルは observation で fail closed)。llama_swap 経路の mission #6 では exec_command が動いたため Landlock 単独起因ではない — strace で切り分け要

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


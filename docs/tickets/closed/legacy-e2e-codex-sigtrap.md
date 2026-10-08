---
id: legacy-e2e-codex-sigtrap
title: codex 子の tool 実行基盤が SIGTRAP 死する問題は是正済
status: 是正済
priority: 未設定
opened: null
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-codex-sigtrap]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-codex-sigtrap] codex+chatgpt の improve worker 環境下で codex の tool 実行基盤 (exec_command) が SIGTRAP 死し MCP tool も呼べない (mission #7 実測、モデルは observation で fail closed)。llama_swap 経路の mission #6 では exec_command が動いたため Landlock 単独起因ではない — strace で切り分け要

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。真因は Landlock でなく RLIMIT_AS で、as_mb 引き上げで解消 (b8004d9)。 根拠: b8004d9。真因は RLIMIT_AS 4096MB で as_mb を 256GB に引き上げ、実機解決 (#74)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: codex 子の tool 実行基盤が SIGTRAP 死する問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。真因は Landlock でなく RLIMIT_AS で、as_mb 引き上げで解消 (b8004d9)。 根拠: b8004d9。真因は RLIMIT_AS 4096MB で as_mb を 256GB に引き上げ、実機解決 (#74)。

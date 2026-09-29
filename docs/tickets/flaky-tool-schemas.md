---
id: flaky-tool-schemas
status: 実装待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [flaky-tool-schemas]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[flaky-tool-schemas] `tests/tools/test_tool_impls.py::test_all_tools_have_schemas` がフル suite 下で ERROR (setup 段、3 回中 2 回)、単体は pass。順序依存。他 flaky 2 本と束で処理

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


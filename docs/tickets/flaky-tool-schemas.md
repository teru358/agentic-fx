---
id: flaky-tool-schemas
title: 全ツールのスキーマテストがフル suite でだけ ERROR になる
status: 実装待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [flaky-tool-schemas]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[flaky-tool-schemas] `tests/tools/test_tool_impls.py::test_all_tools_have_schemas` がフル suite 下で ERROR (setup 段、3 回中 2 回)、単体は pass。順序依存。他 flaky 2 本と束で処理

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 全ツールのスキーマテストがフル suite でだけ ERROR になる — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 順序依存と記録されているが、現在の再現有無は未確認。 根拠: tests/tools/test_tool_impls.py:373 のテストは現存。順序依存の ERROR が今も出るかは未再現。

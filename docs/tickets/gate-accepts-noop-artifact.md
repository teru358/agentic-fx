---
id: gate-accepts-noop-artifact
status: 是正済
priority: 未設定
opened: 2026-09-01
closed: 2026-09-01
related: [research-facts-are-selectable-work]
backfilled: true
source_section: 是正済み
---
# [gate-accepts-noop-artifact]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[gate-accepts-noop-artifact]/[research-facts-are-selectable-work] 是正済 `cd3ba1c` (2026-09-01) — noop_gate (AST+config 同一 → `noop_copy_of:<name>` / test_* < `improve.gate.min_test_functions`=3 → `self_test_too_thin`)、discoveries.kind task|fact (fact → backlog note、選択不可)、`afx backlog note <id>`。m34/m40 は `noop_copy_of:_examples/rsi_indicator`

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


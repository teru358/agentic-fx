---
id: flake
title: 全スイート負荷時に対話停止テストが落ちる (タイミング依存)
status: 是正済
priority: 低
opened: null
closed: 2026-10-03
related: []
backfilled: true
source_section: 未完了
---
# [flake]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[flake] test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end が全スイート負荷下でのみ fail (単独安定 pass)。shell_interrupt と同型のタイミング依存

## 修正内容

- 2026-10-03: stop_event の起動を service_started の観測に、上限は起動後から計測

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 全スイート負荷時に対話停止テストが落ちる (タイミング依存) — 仕分け (2026-10-02、現物で成立を確認): 単独では安定 pass、負荷耐性のある待ち方への修正が未着手。 根拠: tests/test_service_app.py:2470 のテストが現存、負荷下タイミング依存の記述のみ。
- 2026-10-03: 是正内容 — stop_event の起動を service_started の観測に、上限は起動後から計測

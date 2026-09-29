---
id: flake
status: 実装待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [flake]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[flake] test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end が全スイート負荷下でのみ fail (単独安定 pass)。shell_interrupt と同型のタイミング依存

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


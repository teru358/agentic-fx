---
id: test-init-offline-unreachable-bridge-flake
status: 設計待ち
priority: 未設定
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [test-init-offline-unreachable-bridge-flake]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[test-init-offline-unreachable-bridge-flake] 起票 2026-09-27 (flake)**: `tests/test_init_and_guard.py::test_init_completes_offline_with_unreachable_bridge` が単独では pass、フルスイート負荷下で fail (ofc / small-fixes 両ブランチのフルスイートで再現、main 由来のテスト)。他 flake 4 種と束で処理

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


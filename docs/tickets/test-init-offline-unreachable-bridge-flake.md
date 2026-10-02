---
id: test-init-offline-unreachable-bridge-flake
title: init の offline テストがフル suite の負荷下で落ちる
status: 設計待ち
priority: 低
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [test-init-offline-unreachable-bridge-flake]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[test-init-offline-unreachable-bridge-flake] 起票 2026-09-27 (flake)**: `tests/test_init_and_guard.py::test_init_completes_offline_with_unreachable_bridge` が単独では pass、フルスイート負荷下で fail (ofc / small-fixes 両ブランチのフルスイートで再現、main 由来のテスト)。他 flake 4 種と束で処理

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: init の offline テストがフル suite の負荷下で落ちる — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 単独では pass、フルスイートで fail。他 flake と束で処理。 根拠: tests/test_init_and_guard.py:245 のテストは現存。負荷下の再現は未確認。

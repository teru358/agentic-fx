---
id: flake-shell-interrupt-timing
status: 実装待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [flake-shell-interrupt-timing]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[起票] `tests/test_shell_interrupt.py` は別 pytest 並走でタイミング依存 fail (単独 11 passed) — 負荷耐性のある待ち方に直す (プラン 10 外)。同ファイルは `select()` の FD_SETSIZE 1024 に脆く無変異でも落ちる (束 E 段 0 で実測)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


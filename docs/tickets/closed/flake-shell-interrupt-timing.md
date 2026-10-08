---
id: flake-shell-interrupt-timing
title: 別 pytest 並走で shell 割り込みテストが落ちる
status: 是正済
priority: 低
opened: null
closed: 2026-10-03
related: []
backfilled: true
source_section: 未完了
---
# [flake-shell-interrupt-timing]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[起票] `tests/test_shell_interrupt.py` は別 pytest 並走でタイミング依存 fail (単独 11 passed) — 負荷耐性のある待ち方に直す (プラン 10 外)。同ファイルは `select()` の FD_SETSIZE 1024 に脆く無変異でも落ちる (束 E 段 0 で実測)

## 修正内容

- 2026-10-03: shell.py を select.poll に (fd 1024 超で落ちる本番欠陥を pin)、固定 sleep を条件待ちに

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 別 pytest 並走で shell 割り込みテストが落ちる — 仕分け (2026-10-02、現物で成立を確認): 負荷耐性のある待ち方に直す案、未着手。 根拠: tests/test_shell_interrupt.py が現存、select() の FD_SETSIZE 1024 脆弱性は本文の実測のみ。
- 2026-10-03: 是正内容 — shell.py を select.poll に (fd 1024 超で落ちる本番欠陥を pin)、固定 sleep を条件待ちに

---
id: realbackend-tests-fail-under-claude-code
title: Claude Code の Bash から実 backend テスト 2 本が handshake 失敗
status: 設計待ち
priority: 低
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [realbackend-tests-fail-under-claude-code]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[realbackend-tests-fail-under-claude-code] 起票 2026-09-27 (環境)**: `tests/loops/test_verify_backend_realbackend.py` の claude / codex 2 本が main でも `mission never reached ready (handshake failed)` で 2 秒で fail (Claude Code の Bash から実行したとき)。ユーザーの tmux から走らせた 9/27 の 4675 passed には含まれていたか未確認。原因 = 入れ子の CLI 起動か env (`CLAUDE_CODE_*`) の伝播と推定、未調査

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: Claude Code の Bash から実 backend テスト 2 本が handshake 失敗 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 入れ子 CLI 起動か env 伝播が原因と推定、未調査。 根拠: tests/loops/test_verify_backend_realbackend.py は現存。Claude Code の Bash から実行した再現が要る。

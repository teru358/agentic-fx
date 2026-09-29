---
id: realbackend-tests-fail-under-claude-code
status: 設計待ち
priority: 未設定
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [realbackend-tests-fail-under-claude-code]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[realbackend-tests-fail-under-claude-code] 起票 2026-09-27 (環境)**: `tests/loops/test_verify_backend_realbackend.py` の claude / codex 2 本が main でも `mission never reached ready (handshake failed)` で 2 秒で fail (Claude Code の Bash から実行したとき)。ユーザーの tmux から走らせた 9/27 の 4675 passed には含まれていたか未確認。原因 = 入れ子の CLI 起動か env (`CLAUDE_CODE_*`) の伝播と推定、未調査

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


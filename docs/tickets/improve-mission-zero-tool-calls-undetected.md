---
id: improve-mission-zero-tool-calls-undetected
status: 裁定待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [improve-mission-zero-tool-calls-undetected]

**状態**: 裁定待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[improve-mission-zero-tool-calls-undetected] (重要、同 観測 B)** registry の tool 呼び出し 0 件で終端した improve mission が「agent が自主的に observation を選んだ」と区別できない (activity / DB / counters に痕跡なし)。是正案: `mission_observation` 等の activity に `tool_calls=N` を付ける + CLI stderr の既知致命パターン (`code-mode host exited` / `SIGTRAP` / `closed its stdout`) を `cli_stderr_fatal` として activity 化。無人運転の前提条件 **→ 是正済 `838b09d`+`244559c` で tool_calls=N / cli_stderr_fatal、実機未確認** **実機確認済 (A4 11 回目): claude #72 `tool_calls=13` (transcript と一致)、codex #73 `cli_stderr_fatal` + `tool_calls=0` で無力化が activity に出た → クローズ**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


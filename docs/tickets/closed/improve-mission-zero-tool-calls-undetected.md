---
id: improve-mission-zero-tool-calls-undetected
title: improve mission のツール呼び出し 0 件が検知できない (是正済)
status: 是正済
priority: 未設定
opened: null
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [improve-mission-zero-tool-calls-undetected]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[improve-mission-zero-tool-calls-undetected] (重要、同 観測 B)** registry の tool 呼び出し 0 件で終端した improve mission が「agent が自主的に observation を選んだ」と区別できない (activity / DB / counters に痕跡なし)。是正案: `mission_observation` 等の activity に `tool_calls=N` を付ける + CLI stderr の既知致命パターン (`code-mode host exited` / `SIGTRAP` / `closed its stdout`) を `cli_stderr_fatal` として activity 化。無人運転の前提条件 **→ 是正済 `838b09d`+`244559c` で tool_calls=N / cli_stderr_fatal、実機未確認** **実機確認済 (A4 11 回目): claude #72 `tool_calls=13` (transcript と一致)、codex #73 `cli_stderr_fatal` + `tool_calls=0` で無力化が activity に出た → クローズ**

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。全終端 activity に tool_calls=N、致命 stderr で cli_stderr_fatal を出す。実機確認してクローズ済み。 根拠: 838b09d + 244559c。tool_calls=N と cli_stderr_fatal を activity に出し、A4 11 回目で実機確認済み。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: improve mission のツール呼び出し 0 件が検知できない (是正済) — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。全終端 activity に tool_calls=N、致命 stderr で cli_stderr_fatal を出す。実機確認してクローズ済み。 根拠: 838b09d + 244559c。tool_calls=N と cli_stderr_fatal を activity に出し、A4 11 回目で実機確認済み。

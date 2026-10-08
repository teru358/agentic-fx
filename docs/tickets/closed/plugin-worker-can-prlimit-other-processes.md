---
id: plugin-worker-can-prlimit-other-processes
title: plugin worker から同 uid の他プロセスの rlimit を書き換えられる
status: 是正済
priority: 高
opened: 2026-10-03
closed: 2026-10-05
related: []
---
# [plugin-worker-can-prlimit-other-processes] plugin worker から同 uid の他プロセスの rlimit を書き換えられる

**状態**: 是正済 / **優先**: 高

## 現象

plugin worker (候補 strategy の backtest、live の signal 計算) は Landlock が無く seccomp も無いため、prlimit64 で同じ uid の他プロセス (afx 本体を含む) の RLIMIT_CPU 等を書き換えられる。setpriority / sched_setaffinity / pidfd_open も通る。2026-10-03 実測 (tmp/design-plugin-landlock/measure/probe4_*.log)。afx の RLIMIT_CPU を下げてサービスを落とす経路。

## 原因

## 処置案・裁定

plugin worker Landlock の束 (tmp/design-plugin-landlock/C0.md v0.4 §6) の seccomp で、prlimit64 / setpriority / sched_setaffinity / pidfd_open / kill / tgkill を「pid が 0 か自分」のときだけ許可する。gate pytest worker にも同じ filter を掛ける (別チケット gate-pytest-dev-writable-and-ldso-exec と同時)。

## 修正内容

- 2026-10-05: plugin worker 隔離 (Landlock + seccomp allow/6 + source-only loader + 二段 protocol + 共通 admission)。spec docs/superpowers/specs/2026-10-04-plugin-worker-sandbox-design.md v1.3

## 経緯

- 2026-10-03: 起票。
- 2026-10-04: 状態: 実装待ち — 2026-10-04: 設計 spec v1.0 (docs/superpowers/specs/2026-10-04-plugin-worker-sandbox-design.md、設計レビュー 8 周 + 受入周) をユーザー承認。実装は T1 ∥ T2 → T3 → T4 → T5。
- 2026-10-05: 是正内容 — plugin worker 隔離 (Landlock + seccomp allow/6 + source-only loader + 二段 protocol + 共通 admission)。spec docs/superpowers/specs/2026-10-04-plugin-worker-sandbox-design.md v1.3

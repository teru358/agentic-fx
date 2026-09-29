---
id: bootstrap-probe-tests-mkdir-real-logs-dir
status: 是正済
priority: 中
opened: 2026-09-12
closed: 2026-09-12
related: [tests-touching-real-repo-resources]
backfilled: true
source_section: 未完了
---
# [bootstrap-probe-tests-mkdir-real-logs-dir]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[bootstrap-probe-tests-mkdir-real-logs-dir] **是正済 test-hygiene T1 (2026-09-12、値コピー import 解消 + env `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR` + repo-root session guard、犯人は `_drive_main` の chdir 漏れ)** — 旧: (中) `tests/test_mission_worker*.py` の `_run_bootstrap_probe` 系は実 subprocess を spawn し、子の `_bootstrap_improve_profile` が **実 `logs/mission-transcripts` を mkdir** する (import 時束縛が session fixture の monkeypatch を素通り)。fresh worktree では session guard `_guard_real_mission_transcripts_dir_is_never_touched` が ERROR、本体 repo では既存 dir に隠れる。同型: runner テストが cwd に `mcp.json` / `schema.json` を書き残す (worktree `tmp/wt/cx` で観測)。[[tests-touching-real-repo-resources]] の再演 — 子プロセスの root を tmp_path に向ける 追加観測 2026-09-12: T-D の `_write_prompt_file` テストのどれかが cwd (repo root) に `prompt.txt` (1 byte, 0600) を残す。cwd 依存のテストを tmp_path に寄せる束で一緒に

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


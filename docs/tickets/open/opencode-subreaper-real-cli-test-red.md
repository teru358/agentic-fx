---
id: opencode-subreaper-real-cli-test-red
title: 実 opencode CLI の subreaper test が main で赤
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-13
closed: null
related: [live-confirmed-bars-into-history]
---
# [opencode-subreaper-real-cli-test-red] 実 opencode CLI の subreaper test が main で赤

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

tests/core/test_cli_subreaper_real_cli.py::test_real_cli_subreaper_and_orphans[opencode] が main (2026-11-13) で単独でも失敗する (assert set() == {pid}、孤児 pid を観測できない)。実 opencode v2.3.0 の process の振る舞いが変わった可能性。他の 3 backend は緑。あわせて tests/runners/test_worker_runner.py の 2 本は mission の prompt に "hi" を使っており、worktree の path に hi を含むと argv の漏れ guard が誤検知して赤になる (histguard で発生)。prompt を path と衝突しない値に変える。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。
- 2026-11-13: 種別: 保守、関連: [live-confirmed-bars-into-history] — tests/runners/test_worker_runner.py の Mission の prompt 既定値 "hi" (8 か所) を path と衝突しない文字列に替えた (worktree 名に hi を含むと argv の漏れ guard が誤検知していた)。同 file 99 緑。残りは実 opencode v2.3.0 の subreaper test。

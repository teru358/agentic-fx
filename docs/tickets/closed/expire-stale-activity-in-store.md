---
id: expire-stale-activity-in-store
status: 是正済
priority: 未設定
opened: 2026-09-27
closed: 2026-09-28
related: [friday-cutoff-validation, cli-bless-runtime-error-traceback]
backfilled: true
source_section: 是正済み
---
# [expire-stale-activity-in-store]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[expire-stale-activity-in-store] / [friday-cutoff-validation] / [cli-bless-runtime-error-traceback] **是正済 (branch `small-fixes-20260927` `eafaef9`、2026-09-27、main ff は 9/28 開場観測後)** — expire_stale は `ExpireResult` を返し service が activity を書く (RETURNING は位置読み) / `friday_swing_cutoff_ny` は ASCII HH:MM かつ `< 16:00` (21:00 UTC = NY 17:00 EDT / 16:00 EST) + 旧暦コメント 3 箇所 / bless の hash 不一致は `LiveHashMismatchAfterSwitchError` `VersionHashMismatchError` (RuntimeError 派生) で CLI は専用例外 + HistoryGitError だけを rc=1 + 状態を断定しない案内 (approval list → 版の復旧 → approval retry / reject) に。段 0 30 変異 (生存 2 → pin)、codex terra r1×2 + r2、ローカル 3 本 (Y1 → pin)。記録 `tmp/small-20260927/`

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


---
id: retry-switched-approves-without-deploy
status: 是正済
priority: 未設定
opened: 2026-09-20
closed: 2026-09-19
related: [cli-bless-unresolved-journal, switch-not-required-skips-hash-verify, switch-ops-hardening, reject-revert-without-live-classification, cli-bless-runtime-error-traceback, switch-ops-code-review-r2-altitude, switch-ops-r2b-low-unpinned]
backfilled: true
source_section: 是正済み
---
# [retry-switched-approves-without-deploy]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[retry-switched-approves-without-deploy] / [cli-bless-unresolved-journal] / [switch-not-required-skips-hash-verify] 是正済 [switch-ops-hardening] (2026-09-20、main `e16eea0`、設計書 `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md` v1.8c): `switched` 行の再開は live を分類してから (案 C、not_switched は巻き戻して再実行・foreign は触らず待機) / reconcile の 3 経路は lock 内で行と分類を読み直し commit まで lock 内 / シェルの `approve`・`approval retry` は lock 内で確定した outcome を文言に写す / `approval list` / CLI bless・retire の `UnresolvedJournalError` は rc=1 + 次の一手 / 再開時に `(switch_required, old_kind, old_target)` が保存値と食い違えば巻き戻して新 op_id (2 周目 /code-review #1、probe で再現) / 版 dir の hash 照合を切替の要否にかかわらず `_finalize_decision` の前に通す。残余は設計書 §5 と ticket [reject-revert-without-live-classification] [cli-bless-runtime-error-traceback] [switch-ops-code-review-r2-altitude] [switch-ops-r2b-low-unpinned]

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


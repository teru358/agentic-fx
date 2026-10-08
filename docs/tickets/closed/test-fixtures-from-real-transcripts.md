---
id: test-fixtures-from-real-transcripts
status: 是正済
priority: 未設定
opened: 2026-08-31
closed: 2026-08-31
related: []
backfilled: true
source_section: 是正済み
---
# [test-fixtures-from-real-transcripts]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-resume-discard] 是正済 `f0e9f87`/`5705f55`/`5dd7fd0` (2026-08-31) — 追撃回収の discard 理由をマーカー記録、追撃 stderr を保持、`_last_step_finish_reason` が `event["part"]["reason"]` を読むよう是正 (偽形状フィクスチャが根因、メモリ [[test-fixtures-from-real-transcripts]])。m28 で受理を実機確認 (残は観測性節)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


---
id: signal-producer-reads-import-table-under-mt5-primary
status: 是正済
priority: 高
opened: 2026-09-28
closed: 2026-09-28
related: [live-storage-source-mapping-scattered, signal-producer-catchup-requires-latest-tail]
backfilled: true
source_section: 是正済み
---
# [signal-producer-reads-import-table-under-mt5-primary]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

[signal-producer-reads-import-table-under-mt5-primary] **是正済 main `bb4961d` (2026-09-28、push 済)** — `_run_signal_maintenance` が producer にライブ保存名 (mt5 → mt5-live) を渡す。実 producer + 実 reader の配線テスト (修正前 red)。段 0 3 変異 KILLED / codex terra r1 I1 (monkeypatch → 委譲) / ローカル 3 本 14 件 → Y0。残: [live-storage-source-mapping-scattered] (8 箇所集約) / [signal-producer-catchup-requires-latest-tail]。記録 `tmp/design-prodsrc/`

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


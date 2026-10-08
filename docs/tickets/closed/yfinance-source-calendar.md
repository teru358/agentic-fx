---
id: yfinance-source-calendar
status: 是正済
priority: 中
opened: 2026-09-28
closed: 2026-09-28
related: []
backfilled: true
source_section: 是正済み
---
# [yfinance-source-calendar]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

上記の第 2 段 **[yfinance-source-calendar] 起票 2026-09-28 (中、ユーザー要望)**: yfinance は日曜 21:00〜23:00 UTC の足を持たない。source 別の開場時刻を暦に持たせ、`_is_stalled` / `Ingest.prepare` / `health.validate_bars` の gap 検査 (`_closed_minutes`) / scheduler の `session_start` を含む暦契約を揃える (astra: 第 2 段は controller 単体でなく全体で)。記録 `tmp/design-outage-stalled/`

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


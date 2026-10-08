---
id: market-calendar-broker-mismatch
status: 是正済
priority: 未設定
opened: 2026-09-06
closed: 2026-09-06
related: []
backfilled: true
source_section: 完了ログ
---
# [market-calendar-broker-mismatch]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[market-calendar-broker-mismatch] 是正済 `6cd4934` + `74a23ec` (祝日 = 取引日ラベル)、1 周目レビュー済** (2026-09-06 CP8 で発見、Critical): OANDA Japan MT5 は UTC+3 固定で週末境界 21:00 UTC (DST 非追従)。`market_hours` の NY 17:00 暦は冬に 1 時間ずれ、replay が足の無い時刻に発注して ValueError で全停止。是正 = market_hours を 21:00 UTC 固定 + 祝日 12/25・1/1、replay に fail-soft (proposal_dropped_no_bar)。

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


---
id: a23c-lite-auto-resume-when-flat
title: 建玉・指値ゼロの episode は degraded から自動で ready に戻す
status: 是正済
priority: 高
opened: 2026-09-29
closed: 2026-09-29
related: [outage-observe-closed-guard-unpinned]
backfilled: true
source_section: 是正済み
---
# [a23c-lite-auto-resume-when-flat]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**A2-3c-lite (建玉も指値も無い episode の自動 ready) 完結 main (2026-09-29、push 済)** — `datafeed.outage.auto_resume_when_flat` (既定 true) / `ready_confirm_ticks` (既定 3)。flat = executor `_EXPOSURE` 全状態 0 件、健全 = 既存契約、streak は失敗/empty/停滞/新 episode/再起動/disabled で 0、`pending_human_confirmation` は自動解除しない、activity `datafeed_recovered_auto`。段 0 15 変異 (生存 4 → pin 3 + 等価 1) / codex sol r1 C1 (flat が OPEN/PENDING_FILL のみ → 是正) + M1 / sol r2 C0 / ローカル 3 本 25 件 → Y2 (pin)。spec §3.1 v1.2。記録 `tmp/design-a2/a23c-lite/`。**実機受入 = 9/30 06:00 JST 過ぎのロールオーバーで degraded → 数分で datafeed_recovered_auto → cron 再開**。残: [outage-stalled-on-broker-daily-rollover-gap] の窓設計 (欠落足の開始時刻で判定)、`observe` の閉場 return が未 pin (範囲外、低: [outage-observe-closed-guard-unpinned])

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-09-30: 題名: 建玉・指値ゼロの episode は degraded から自動で ready に戻す — 実機受入 合格: 9/29 21:17 UTC datafeed_degraded (epoch 3、1m 停滞) → 21:33 datafeed_recovered_auto (streak 3、人手なし) → 21:36 mission 再開

---
id: outage-stalled-ignores-closed-hours
status: 是正済
priority: 未設定
opened: 2026-09-28
closed: 2026-09-28
related: [settings-example-deprecated-keys, db-healthcheck-continuous-session-freshness, yfinance-source-calendar]
backfilled: true
source_section: 是正済み
---
# [outage-stalled-ignores-closed-hours]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[outage-stalled-ignores-closed-hours] / [settings-example-deprecated-keys] **是正済 main `ed2de56` (2026-09-28、push 済)** — 停滞判定 (`outage._is_stalled`) と取得予約 (`Ingest.prepare`) を共有 helper `market_hours.next_bar_confirmation` (次に取引される足の確定時刻、閉場を飛ばす) に統一。週明け統合テスト (金曜最終足を最初の tick で取り込み、state は終始 ready、15m 取得は 21:00 と 21:16 の 2 回)。段 0 13 変異 (生存 3 → pin) / codex sol r1 C0/I0/M1 / `/code-review high` 2 件 (1 = ticket [db-healthcheck-continuous-session-freshness]、1 = テスト是正) / ローカル 3 本 40 件 → Y0。example の旧キー 3 つ除去 + caplog pin。**実機受入 = 10/4 21:00 UTC に degraded なし**。残: yfinance の source 別開場時刻 (第 2 段、「yfinance でも最低限運用」要望) は [yfinance-source-calendar] として起票

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


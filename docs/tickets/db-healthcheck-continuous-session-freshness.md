---
id: db-healthcheck-continuous-session-freshness
status: 設計待ち
priority: 中
opened: 2026-09-28
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [db-healthcheck-continuous-session-freshness]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[db-healthcheck-continuous-session-freshness] 起票 2026-09-28 (中、`/code-review high` stalled r1)**: `service.py:1243` `db_healthcheck` の鮮度式 `bar start + 2×幅 + grace + freshness_max_min` は閉場時間を差し引かず、週明け 21:00〜最初の判断足確定 (15m なら 21:15:30) の間は金曜足しか無いので DataUnhealthy。cron mission はこの窓で起動しないため実害は signal 起動 mission (建玉あり) だけだが、`_is_stalled` / `Ingest` と式が分岐した。`market_hours.next_bar_confirmation` に揃えるか、現状 (開場直後は旧足で判断しない) を仕様として明記するかの裁定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


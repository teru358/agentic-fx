---
id: service-secret-env-leaked-matches-redundant
status: 設計待ち
priority: 未設定
opened: 2026-09-27
closed: null
related: [switch-ops-code-review-r2-altitude]
backfilled: true
source_section: 未完了
---
# [service-secret-env-leaked-matches-redundant]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[service-secret-env-leaked-matches-redundant] 起票 2026-09-27 (同、整理級)**: `service.py` 検査⑤の `leaked` list と `matches` dict が常に同期して埋まり冗長。`matches` に一本化 ([switch-ops-code-review-r2-altitude] と同種)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

